import { createHash } from "node:crypto";
import { join } from "node:path";
import type { Context } from "@deepseek-ai/cordis";
import type { Agent } from "@deepseek-ai/dsh-agent";
import type { FileAttachmentRef, ImageAttachmentRef } from "@deepseek-ai/dsh-attachment";
import type { ContentBlock, UserMessage } from "@deepseek-ai/dsh-llm";
import { createUserMessage } from "@deepseek-ai/dsh-llm";
import type {} from "@deepseek-ai/dsh-fs";
import { fileTypeFromBuffer } from "file-type";
import { lookup } from "mime-types";
import { MmpClient, MmpError, media } from "@mmp/client";
import type { Capability, JobDone, JobRequest, MediaHandle, MediaOutput } from "@mmp/protocol";
import { Artifacts, type Artifact } from "./artifacts.js";
import type { Settings } from "./config.js";
import { Transfers, type ByteSource } from "./transfer.js";

declare module "@deepseek-ai/dsh-llm" {
  interface MessageSourceMap {
    mmp: { kind: "mmp"; plugin: "@mmp/deepseek-harness" };
  }
}

export interface RunResult {
  job: Omit<JobDone, "outputs"> & { outputs: Record<string, unknown> };
  node_id: string;
  assets: Artifact[];
}
type AttachmentRef = ImageAttachmentRef | FileAttachmentRef;
export const nodeOf = (jobId: string) => jobId.slice(0, -27);

export class Runtime {
  readonly client: MmpClient;
  readonly transfers: Transfers;
  readonly artifacts: Artifacts;
  private readonly shutdown = new AbortController();
  private readonly pending = new Set<Promise<unknown>>();
  constructor(readonly ctx: Context, readonly config: Settings, fetchImpl?: typeof fetch) {
    this.client = new MmpClient({ baseUrl: config.baseUrl, apiKey: config.apiKey, fetch: fetchImpl });
    this.transfers = new Transfers(config);
    this.artifacts = new Artifacts(join(config.dataDir, "artifacts"), ctx.attachments);
  }
  async start(): Promise<void> { await this.transfers.start(); }
  async close(): Promise<void> {
    this.shutdown.abort(new Error("MMP plugin unloaded"));
    await Promise.allSettled([...this.pending]);
    await this.transfers.close();
  }
  task<T>(signal: AbortSignal, body: (signal: AbortSignal) => Promise<T>, timeoutMs = this.config.timeoutMs): Promise<T> {
    const combined = AbortSignal.any([signal, this.shutdown.signal, AbortSignal.timeout(timeoutMs)]);
    const promise = body(combined);
    this.pending.add(promise);
    void promise.then(() => this.pending.delete(promise), () => this.pending.delete(promise));
    return promise;
  }
  /** 单个任务使用 wait=0 先取得 id，以便停止或超时后显式取消 A 上的工作。 */
  async job(request: JobRequest, signal: AbortSignal): Promise<JobDone> {
    signal.throwIfAborted();
    let id: string | undefined;
    try {
      const first = await this.client.submit({ ...request, wait: 0 }, signal);
      id = first.job_id;
      if (first.status === "done") return first as JobDone;
      if (first.status === "cancelled") throw new Error("MMP job cancelled");
      return await this.client.wait(id, { pollWaitSec: 10, timeoutMs: this.config.timeoutMs, signal });
    } catch (error) {
      if (id) await this.client.cancel(id, 0, AbortSignal.timeout(5000)).catch(() => undefined);
      throw error;
    }
  }
  async source(ref: AttachmentRef, signal: AbortSignal): Promise<ByteSource> {
    const store = this.ctx.attachments;
    if ("mediaType" in ref) return {
      bytes: ref.bytes, contentType: ref.mediaType,
      async *open(readSignal) { yield (await store.readImage(ref, readSignal)).data; },
    };
    const source: ByteSource = { bytes: ref.bytes, contentType: "application/octet-stream", open: (readSignal) => store.readFileStream(ref, readSignal) };
    source.contentType = await detect(source, ref.name, signal);
    return source;
  }
  private refs(agent: Agent): AttachmentRef[] {
    return agent.session.deriveMessages().flatMap((message) => message.content.flatMap((block) => block.type === "image" || block.type === "file" ? [block.attachment] : []));
  }
  private async argument(input: unknown, agent: Agent, signal: AbortSignal): Promise<{ source?: ByteSource; handle?: MediaHandle }> {
    if (!isRecord(input)) throw new Error("media input must be an object");
    const kinds = ["attachment_id", "asset_id", "file_path", "get", "ref"].filter((key) => input[key] !== undefined);
    if (kinds.length !== 1) throw new Error("media input requires exactly one of attachment_id, asset_id, file_path, get, ref");
    if (typeof input.attachment_id === "string") {
      const ref = this.refs(agent).find((ref) => ref.attachmentId === input.attachment_id);
      if (!ref) throw new Error("attachment_id is not referenced by the current session context");
      return { source: await this.source(ref, signal) };
    }
    if (typeof input.asset_id === "string") {
      const asset = await this.artifacts.get(input.asset_id);
      if (asset.sessionId !== agent.session.id && !this.refs(agent).some((ref) => ref.attachmentId === asset.attachment.attachmentId)) throw new Error("asset_id does not belong to this session");
      return { source: { bytes: asset.attachment.bytes, contentType: asset.contentType, open: (readSignal) => this.ctx.attachments.readFileStream(asset.attachment, readSignal) } };
    }
    if (typeof input.file_path === "string") {
      const target = await this.ctx.fs.resolve(input.file_path, { cwd: agent.session.header.cwd, signal });
      const info = await this.ctx.fs.stat(target, signal);
      if (!info || info.type !== "file" || info.size === undefined) throw new Error("file_path must resolve to a regular file with a known size");
      const fs = this.ctx.fs;
      const size = info.size;
      const source: ByteSource = { bytes: size, contentType: "application/octet-stream", async *open(readSignal) {
        for (let offset = 0; offset < size;) {
          readSignal.throwIfAborted();
          const chunk = await fs.readByteRange(target, { offset, length: Math.min(64 * 1024, size - offset) }, readSignal);
          if (!chunk.length) throw new Error("input file changed during transfer");
          yield chunk;
          offset += chunk.length;
        }
      } };
      source.contentType = await detect(source, input.file_path, signal);
      return { source };
    }
    if (typeof input.ref === "string") return { handle: media.ref(input.ref) };
    if (isRecord(input.get) && typeof input.get.url === "string") {
      const url = new URL(input.get.url);
      if (!/^https?:$/.test(url.protocol) || url.username || url.password) throw new Error("get.url must be HTTP(S) without URL credentials");
      if (input.get.headers !== undefined && (!isRecord(input.get.headers) || Object.values(input.get.headers).some((value) => typeof value !== "string"))) throw new Error("invalid get.headers");
      return { handle: media.get(url.href, input.get.headers as Record<string, string> | undefined, typeof input.content_type === "string" ? input.content_type : undefined) };
    }
    throw new Error("invalid media input");
  }
  private async handle(source: ByteSource, signal: AbortSignal, releases: (() => unknown)[]): Promise<MediaHandle> {
    if (source.bytes > this.config.maxTransferBytes) throw new Error("media exceeds maxTransferBytes");
    if (source.bytes <= 8 * 1024 * 1024) {
      const chunks: Uint8Array[] = [];
      let count = 0;
      for await (const chunk of source.open(signal)) {
        signal.throwIfAborted(); count += chunk.byteLength;
        if (count > source.bytes) throw new Error("input size mismatch");
        chunks.push(chunk);
      }
      if (count !== source.bytes) throw new Error("input size mismatch");
      return media.inline(Buffer.concat(chunks), source.contentType);
    }
    const ticket = this.transfers.input(source, signal);
    releases.push(ticket.release);
    return media.get(ticket.endpoint.url, undefined, source.contentType);
  }
  async preflight(messages: UserMessage[], signal: AbortSignal): Promise<UserMessage[]> {
    const result: UserMessage[] = [];
    for (const message of messages) {
      result.push(message);
      const seen = new Set<string>();
      for (const block of message.content) {
        if (block.type !== "image" && block.type !== "file") continue;
        if (seen.has(block.attachment.attachmentId)) continue;
        seen.add(block.attachment.attachmentId);
        const releases: (() => unknown)[] = [];
        let text: string;
        try {
          text = await this.task(signal, async (active) => {
            const source = await this.source(block.attachment, active);
            const capability = await this.client.resolveCapability({ purpose: "triage", contentType: source.contentType }, active);
            const fields = capability.input.fields.filter((field) => field.kind === "media");
            if (fields.length !== 1) throw new Error("triage must declare one media field");
            const field = fields[0]!;
            const handle = await this.handle(source, active, releases);
            const done = await this.job({ type: capability.id, arguments: { [field.name]: field.kind === "media" && field.cardinality.max > 1 ? [handle] : handle }, priority: "interactive" }, active);
            if (!done.agent_context) throw new Error("triage completed without agent_context");
            return `${done.agent_context.text}\nMMP attachment_id=${block.attachment.attachmentId} node_id=${nodeOf(done.job_id)} media_ids=${JSON.stringify(done.media_ids)}`;
          }, this.config.preflightTimeoutMs);
        } catch (error) {
          signal.throwIfAborted();
          this.shutdown.signal.throwIfAborted();
          text = `[mmp:agent-context unavailable]\nattachment_id=${block.attachment.attachmentId} reason=${errorCode(error)}。本附件没有可用的 MMP 预检信息；不得假定已转写或已理解。可按需使用其他可用工具。`;
        } finally { for (const release of releases) release(); }
        result.push(createUserMessage({ content: [{ type: "text", text }], source: { kind: "mmp", plugin: "@mmp/deepseek-harness" } }));
      }
    }
    return result;
  }
  async run(input: { type: string; arguments: Record<string, unknown>; node_id?: string; tier?: JobRequest["tier"] }, agent: Agent, signal: AbortSignal): Promise<RunResult> {
    const capability = await this.client.describe(input.type, signal);
    const releases: (() => unknown)[] = [];
    const args = { ...input.arguments };
    const deliveryTickets: ReturnType<Transfers["output"]>[] = [];
    try {
      for (const field of capability.input.fields) {
        if (field.kind !== "media" || args[field.name] === undefined) continue;
        const values = field.cardinality.max === 1 ? [args[field.name]] : args[field.name];
        if (!Array.isArray(values)) throw new Error(`${field.name} must be an array`);
        const handles: MediaHandle[] = [];
        for (const value of values) {
          const resolved = await this.argument(value, agent, signal);
          if (resolved.handle?.ref && !input.node_id) throw new Error("ref input requires node_id from the original MMP result");
          handles.push(resolved.handle ?? await this.handle(resolved.source!, signal, releases));
        }
        args[field.name] = field.cardinality.max === 1 ? handles[0] : handles;
      }
      let delivery: JobRequest["delivery"];
      if (capability.purpose === "media.deliver") {
        if (!Array.isArray(args.media)) throw new Error("media.deliver requires a media array");
        for (const _ of args.media) {
          const ticket = this.transfers.output(undefined, signal);
          deliveryTickets.push(ticket); releases.push(ticket.release);
        }
        if (!deliveryTickets.length) throw new Error("media.deliver requires at least one media ref");
        delivery = { media: [deliveryTickets[0]!.endpoint, ...deliveryTickets.slice(1).map((ticket) => ticket.endpoint)] };
      }
      const done = await this.job({ ...input, arguments: args, ...(delivery ? { delivery } : {}), priority: "interactive" }, signal);
      try { return await this.materialize(done, capability, agent.session.id, signal, deliveryTickets); }
      catch {
        const refs = Object.fromEntries(Object.entries(done.outputs).flatMap(([field, value]) => {
          const mediaOutputs = (Array.isArray(value) ? value : [value]).filter((item) => item.kind === "media");
          return mediaOutputs.length ? [[field, mediaOutputs.map((item) => ({ ref: (item as MediaOutput).media_id }))]] : [];
        }));
        throw new Error(`MMP task completed, but host media delivery failed. Do not regenerate; retry media.deliver with node_id=${nodeOf(done.job_id)}, job_id=${done.job_id}, refs=${JSON.stringify(refs)}`);
      }
    } finally { for (const release of releases) await release(); }
  }
  private async materialize(done: JobDone, capability: Capability, sessionId: string, signal: AbortSignal, delivered: ReturnType<Transfers["output"]>[] = []): Promise<RunResult> {
    const assets: Artifact[] = [];
    const nodeId = nodeOf(done.job_id);
    const outputs: Record<string, unknown> = {};
    for (const field of capability.output.fields) {
      const value = done.outputs[field.name];
      if (value === undefined) continue;
      if (field.kind !== "media") { outputs[field.name] = value; continue; }
      const items = Array.isArray(value) ? value : [value];
      const materialized = [];
      for (const [index, output] of items.entries()) {
        if (output.kind !== "media") throw new Error(`unexpected non-media output: ${field.name}`);
        if (output.bytes > this.config.maxTransferBytes) throw new Error("media exceeds maxTransferBytes");
        let bytes: AsyncIterable<Uint8Array>;
        let ticket: ReturnType<Transfers["output"]> | undefined;
        try {
          if (output.delivered && delivered[index]) {
            bytes = delivered[index]!.open(output);
          } else if (output.inline !== undefined) {
            const data = Buffer.from(output.inline, "base64");
            if (data.length !== output.bytes || createHash("sha256").update(data).digest("hex") !== output.sha256) throw new Error("inline output integrity mismatch");
            bytes = (async function* () { yield data; })();
          } else {
            ticket = this.transfers.output(output, signal);
            const deliver = await this.client.resolveCapability({ purpose: "media.deliver", contentType: output.content_type }, signal);
            await this.job({ type: deliver.id, node_id: nodeId, arguments: { media: [media.ref(output.media_id)] }, delivery: { media: [ticket.endpoint] } }, signal);
            bytes = ticket.open(output);
          }
          signal.throwIfAborted();
          const asset = await this.artifacts.save(bytes, output, { sessionId, field: field.name, index, nodeId }, signal);
          assets.push(asset);
          const { inline: _inline, delivered: _delivered, ...metadata } = output;
          materialized.push({ ...metadata, asset_id: asset.id, attachment_id: asset.attachment.attachmentId });
        } finally { await ticket?.release(); }
      }
      outputs[field.name] = Array.isArray(value) ? materialized : materialized[0];
    }
    return { job: { ...done, outputs }, node_id: nodeId, assets };
  }
}

export function isRecord(value: unknown): value is Record<string, unknown> { return typeof value === "object" && value !== null && !Array.isArray(value); }
export function errorCode(error: unknown): string { return error instanceof MmpError ? error.code : error instanceof Error && error.name === "TimeoutError" ? "timeout" : "preflight_failed"; }
async function detect(source: ByteSource, name: string, signal: AbortSignal): Promise<string> {
  const chunks: Uint8Array[] = [];
  let length = 0;
  for await (const chunk of source.open(signal)) {
    const part = chunk.subarray(0, 8192 - length);
    chunks.push(part); length += part.length;
    if (length === 8192) break;
  }
  const prefix = Buffer.concat(chunks);
  // 极短、截断文件最终由 A 校验；这里仅用于能力发现。
  const detected = await fileTypeFromBuffer(prefix).catch(() => undefined);
  return detected?.mime ?? (lookup(name) || "application/octet-stream");
}

export function resultContent(value: RunResult): ContentBlock[] {
  return [
    { type: "text", text: JSON.stringify({ ...value.job, node_id: value.node_id, assets: value.assets.map((asset) => ({ asset_id: asset.id, field: asset.field, index: asset.index, attachment_id: asset.attachment.attachmentId, content_type: asset.contentType, bytes: asset.attachment.bytes })) }) },
    ...value.assets.map((asset): ContentBlock => ({ type: "file", attachment: asset.attachment })),
  ];
}
