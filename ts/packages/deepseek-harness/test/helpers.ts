import { createHash } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Context } from "@deepseek-ai/cordis";
import { AttachmentId, type FileAttachmentRef, type AttachmentStore } from "@deepseek-ai/dsh-attachment";
import type { Agent } from "@deepseek-ai/dsh-agent";
import type { ContentBlock, UserMessage } from "@deepseek-ai/dsh-llm";
import { configure } from "../src/config.js";
import { Runtime } from "../src/runtime.js";

export const hash = (data: Uint8Array) => createHash("sha256").update(data).digest("hex");
export const signal = () => new AbortController().signal;
export const jobId = "local-01J7ZQ9K3W8B6Q4M2N1P5R7S9T";
export const source = { tier: "cpu", engine: "fixture", engine_version: "1", generated_at: "2026-10-22T00:00:00Z", degraded: false };
export function mediaOutput(data: Buffer, type = "audio/wav", inline = false) {
  return { kind: "media", content_type: type, bytes: data.length, sha256: hash(data), media_id: `sha256:${hash(data)}`, ...(inline ? { inline: data.toString("base64") } : {}) };
}

export class MemoryAttachments {
  readonly objects = new Map<string, Buffer>();
  async saveFileStream({ data, name, signal }: { data: AsyncIterable<Uint8Array>; name?: string; signal?: AbortSignal }): Promise<FileAttachmentRef> {
    const chunks: Uint8Array[] = [];
    for await (const chunk of data) { signal?.throwIfAborted(); chunks.push(chunk); }
    const bytes = Buffer.concat(chunks);
    const attachmentId = AttachmentId(hash(bytes));
    this.objects.set(attachmentId, bytes);
    return { attachmentId, name: name ?? "file.bin", bytes: bytes.length };
  }
  async *readFileStream(ref: FileAttachmentRef, signal?: AbortSignal) {
    signal?.throwIfAborted();
    const bytes = this.objects.get(ref.attachmentId);
    if (!bytes || hash(bytes) !== ref.attachmentId || bytes.length !== ref.bytes) throw new Error("corrupt attachment");
    for (let offset = 0; offset < bytes.length; offset += 65536) { signal?.throwIfAborted(); yield bytes.subarray(offset, offset + 65536); }
  }
  async readImage(ref: FileAttachmentRef, signal?: AbortSignal) {
    const chunks = [];
    for await (const part of this.readFileStream(ref, signal)) chunks.push(part);
    return { ref, data: Buffer.concat(chunks) };
  }
  async file(data: Buffer, name: string) { return this.saveFileStream({ data: (async function* () { yield data; })(), name }); }
}

export async function fixture(fetchImpl: typeof fetch, options: Record<string, unknown> = {}) {
  const directory = await mkdtemp(join(tmpdir(), "mmp-dsh-test-"));
  const attachments = new MemoryAttachments();
  const ctx = { attachments } as unknown as Context;
  const config = configure({ baseUrl: "http://mmp.local", transferHost: "127.0.0.1", transferPort: 0, transferBaseUrl: "http://127.0.0.1", dataDir: directory, timeoutMs: 3000, ...options });
  const runtime = new Runtime(ctx, config, fetchImpl);
  await runtime.start();
  let messages: UserMessage[] = [];
  const agent = { session: { id: "test-session", header: { cwd: directory }, deriveMessages: () => messages } } as unknown as Agent;
  return { runtime, attachments, agent, directory, setMessages: (next: UserMessage[]) => { messages = next; },
    close: async () => { await runtime.close(); await rm(directory, { recursive: true, force: true }); } };
}

export function fakeFetch(route: (url: URL, body: any, init: RequestInit) => unknown | Promise<unknown>): typeof fetch {
  return (async (input: string | URL | Request, init: RequestInit = {}) => {
    const result = await route(new URL(String(input)), init.body ? JSON.parse(String(init.body)) : undefined, init);
    return result instanceof Response ? result : Response.json(result);
  }) as typeof fetch;
}

export function capability(id: string, fields: unknown[], outputs: unknown[], purpose = id) {
  return { id, purpose, visibility: "public", tiers: [{ tier: "cpu", engine: "fixture", engine_version: "1", cost: "low" }], input: { fields }, output: { fields: outputs, agent_context: "optional" } };
}
export const mediaField = (name: string, max = 1, accepts = ["*/*"]) => ({ name, kind: "media", required: true, accepts, cardinality: { min: 1, max } });
export const mediaOutField = (name: string, max = 1) => ({ name, kind: "media", required: true, produces: ["*/*"], cardinality: { min: 1, max } });
export const capabilities = (...items: unknown[]) => ({ protocol_version: "3.1", capabilities: items.map((capability) => ({ capability, nodes: ["local"] })) });
export const done = (type: string, outputs: Record<string, unknown>, extra: Record<string, unknown> = {}) => ({ job_id: jobId, type, status: "done", media_ids: {}, outputs, cached: false, source, ...extra });
export const textOf = (messages: { content: readonly ContentBlock[] }[]) => messages.flatMap((message) => message.content.flatMap((block) => block.type === "text" ? [block.text] : [])).join("\n");
