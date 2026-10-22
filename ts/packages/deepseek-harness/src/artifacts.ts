import { randomUUID } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { Readable } from "node:stream";
import type { AttachmentStore, FileAttachmentRef } from "@deepseek-ai/dsh-attachment";
import type { MediaOutput } from "@mmp/protocol";
import { extension } from "mime-types";

export interface Artifact {
  id: string;
  sessionId: string;
  field: string;
  index: number;
  attachment: FileAttachmentRef;
  contentType: string;
  mediaId: string;
  nodeId: string;
  href: string;
}

/** 字节由宿主附件存储持有；这里只保存展示与再次调用所需的映射。 */
export class Artifacts {
  constructor(private readonly directory: string, private readonly attachments: AttachmentStore) {}
  async save(input: AsyncIterable<Uint8Array>, output: MediaOutput, owner: { sessionId: string; field: string; index: number; nodeId: string }, signal: AbortSignal): Promise<Artifact> {
    const suffix = extension(output.content_type) || "bin";
    const attachment = await this.attachments.saveFileStream({ data: input, name: `${owner.field}-${owner.index + 1}.${suffix}`, signal });
    const id = randomUUID();
    const artifact: Artifact = {
      ...owner, id, attachment, contentType: output.content_type, mediaId: output.media_id,
      href: `/api/mmp-media?asset=${id}`,
    };
    await mkdir(this.directory, { recursive: true, mode: 0o700 });
    await writeFile(join(this.directory, `${id}.json`), JSON.stringify(artifact), { flag: "wx", mode: 0o600 });
    return artifact;
  }
  async get(id: string): Promise<Artifact> {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(id)) throw new Error("invalid asset_id");
    return JSON.parse(await readFile(join(this.directory, `${id}.json`), "utf8")) as Artifact;
  }
  /** 由 connection.fetch 调用，沿用宿主认证；支持播放器的 HTTP Range。 */
  async serve(request: Request): Promise<Response> {
    const url = new URL(request.url);
    let artifact: Artifact;
    try { artifact = await this.get(url.searchParams.get("asset") ?? ""); }
    catch { return new Response(null, { status: 404 }); }
    const size = artifact.attachment.bytes;
    const rawRange = request.headers.get("range");
    const range = rawRange === null ? undefined : byteRange(rawRange, size);
    if (range === null) return new Response(null, { status: 416, headers: { "content-range": `bytes */${size}` } });
    const start = range?.start ?? 0;
    const end = range?.end ?? size - 1;
    const inline = /^(image\/(png|jpeg|gif|webp)|audio\/|video\/)/.test(artifact.contentType) && url.searchParams.get("download") !== "1";
    const headers: Record<string, string> = {
      "content-type": artifact.contentType,
      "content-length": String(Math.max(0, end - start + 1)),
      "content-disposition": `${inline ? "inline" : "attachment"}; filename*=UTF-8''${encodeURIComponent(artifact.attachment.name)}`,
      "accept-ranges": "bytes", "cache-control": "private, no-store", "x-content-type-options": "nosniff",
      "content-security-policy": "sandbox; default-src 'none'",
    };
    if (range) headers["content-range"] = `bytes ${start}-${end}/${size}`;
    const stream = this.attachments.readFileStream(artifact.attachment, request.signal);
    const body = request.method === "HEAD" ? null : Readable.toWeb(Readable.from(slice(stream, start, end))) as ReadableStream<Uint8Array>;
    return new Response(body, { status: range ? 206 : 200, headers });
  }
}

function byteRange(value: string, size: number): { start: number; end: number } | null {
  const match = /^bytes=(\d*)-(\d*)$/.exec(value);
  if (!match || (!match[1] && !match[2]) || size === 0) return null;
  const start = match[1] ? Number(match[1]) : Math.max(0, size - Number(match[2]));
  const end = match[1] && match[2] ? Math.min(size - 1, Number(match[2])) : size - 1;
  return Number.isSafeInteger(start) && Number.isSafeInteger(end) && start <= end && start < size ? { start, end } : null;
}

async function* slice(stream: AsyncIterable<Uint8Array>, start: number, end: number): AsyncIterable<Uint8Array> {
  let offset = 0;
  for await (const chunk of stream) {
    const next = offset + chunk.byteLength;
    if (next > start && offset <= end) yield chunk.subarray(Math.max(0, start - offset), Math.min(chunk.length, end - offset + 1));
    offset = next;
    if (offset > end) return;
  }
}
