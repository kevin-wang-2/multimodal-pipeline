import { createHash, randomBytes } from "node:crypto";
import { createServer, type Server, type IncomingMessage, type ServerResponse } from "node:http";
import { createReadStream, createWriteStream } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Readable, Transform } from "node:stream";
import { pipeline } from "node:stream/promises";
import type { Settings } from "./config.js";

export interface ByteSource {
  bytes: number;
  contentType: string;
  open(signal: AbortSignal): AsyncIterable<Uint8Array>;
}
interface Ticket {
  method: "GET" | "PUT";
  signal: AbortSignal;
  serve(req: IncomingMessage, res: ServerResponse): Promise<void>;
}

/** 只供 A 回调：短命随机地址，不进入会话日志；浏览器另走宿主认证接口。 */
export class Transfers {
  private readonly tickets = new Map<string, Ticket>();
  private readonly server: Server;
  private directory = "";
  constructor(private readonly config: Settings) {
    this.server = createServer((req, res) => {
      void this.handle(req, res).catch(() => {
        if (!res.headersSent) res.writeHead(500);
        res.end();
      });
    });
  }
  async start(): Promise<void> {
    this.directory = await mkdtemp(join(tmpdir(), "mmp-dsh-"));
    try {
      await new Promise<void>((resolve, reject) => {
        this.server.once("error", reject);
        this.server.listen(this.config.transferPort, this.config.transferHost, () => {
          this.server.removeListener("error", reject);
          resolve();
        });
      });
    } catch (error) { await rm(this.directory, { recursive: true, force: true }); throw error; }
  }
  get port(): number { return (this.server.address() as { port: number }).port; }
  private ticket(ticket: Ticket) {
    ticket.signal.throwIfAborted();
    const id = randomBytes(24).toString("hex");
    const release = () => { this.tickets.delete(id); ticket.signal.removeEventListener("abort", release); };
    this.tickets.set(id, ticket);
    ticket.signal.addEventListener("abort", release, { once: true });
    const url = new URL(`/mmp-transfer/${id}`, this.config.transferBaseUrl);
    // port=0 仅用于本地测试，部署配置使用固定的内网回调端口。
    if (this.config.transferPort === 0) url.port = String(this.port);
    return { endpoint: { url: url.href }, release };
  }
  input(source: ByteSource, signal: AbortSignal) {
    if (source.bytes > this.config.maxTransferBytes) throw new Error("media exceeds maxTransferBytes");
    return this.ticket({ method: "GET", signal, serve: async (_req, res) => {
      res.writeHead(200, { "content-type": source.contentType, "content-length": source.bytes, "cache-control": "no-store" });
      await pipeline(Readable.from(source.open(signal)), res, { signal });
    } });
  }
  output(expected: { bytes: number; sha256: string } | undefined, signal: AbortSignal) {
    if (expected && expected.bytes > this.config.maxTransferBytes) throw new Error("media exceeds maxTransferBytes");
    const path = join(this.directory, randomBytes(24).toString("hex"));
    let complete = false;
    let active = false;
    let received: { bytes: number; sha256: string } | undefined;
    const limit = expected?.bytes ?? this.config.maxTransferBytes;
    const ticket = this.ticket({ method: "PUT", signal, serve: async (req, res) => {
      if (complete || active) { res.writeHead(409).end(); return; }
      active = true;
      let count = 0;
      const hash = createHash("sha256");
      const verify = new Transform({ transform(chunk: Buffer, _encoding, callback) {
        count += chunk.length;
        if (count > limit) { callback(new Error("output size mismatch")); return; }
        hash.update(chunk); callback(null, chunk);
      } });
      try {
        await pipeline(req, verify, createWriteStream(path, { flags: "w", mode: 0o600 }), { signal });
        received = { bytes: count, sha256: hash.digest("hex") };
        if (expected && (count !== expected.bytes || received.sha256 !== expected.sha256)) throw new Error("output integrity mismatch");
        complete = true;
        res.writeHead(204).end();
      } finally { active = false; }
    } });
    return {
      endpoint: ticket.endpoint,
      open: (metadata?: { bytes: number; sha256: string }) => {
        if (!complete) throw new Error("MMP output was not delivered");
        if (metadata && (metadata.bytes !== received?.bytes || metadata.sha256 !== received.sha256)) throw new Error("output integrity mismatch");
        return createReadStream(path);
      },
      release: async () => { ticket.release(); await rm(path, { force: true }); },
    };
  }
  private async handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const ticket = this.tickets.get((req.url ?? "").replace(/^\/mmp-transfer\//, ""));
    if (!ticket || ticket.signal.aborted) { res.writeHead(404).end(); return; }
    if (req.method !== ticket.method) { res.writeHead(405, { allow: ticket.method }).end(); return; }
    await ticket.serve(req, res);
  }
  async close(): Promise<void> {
    this.tickets.clear();
    const closed = new Promise<void>((resolve) => this.server.close(() => resolve()));
    this.server.closeAllConnections();
    await closed;
    await rm(this.directory, { recursive: true, force: true });
  }
}
