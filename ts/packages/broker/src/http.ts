/** 任务 API 的 HTTP 绑定（node:http，无框架）。只做：认证、解析、校验 JobRequest、调操作、按 { status, body } 回。 */
import type { IncomingMessage, ServerResponse } from "node:http";
import { ApiError, ID, validator, errorsOf } from "@mmp/protocol";
import type { Broker } from "./core.js";

export interface HttpOptions {
  apiKey?: string;
  maxBodyBytes?: number;    // inline 8 MiB 的 base64 ≈ 11 MB，留余量
  log?: (msg: string) => void;
}

export type Handler = (req: IncomingMessage, res: ServerResponse) => void;

export function createHttpHandler(broker: Broker, opts: HttpOptions = {}): Handler {
  const maxBody = opts.maxBodyBytes ?? 16 * 1024 * 1024;
  const validateJob = validator(`${ID.jobApi}#/$defs/JobRequest`);
  const log = opts.log ?? (() => {});

  const reply = (res: ServerResponse, status: number, body: unknown) => {
    const data = Buffer.from(JSON.stringify(body), "utf8");
    res.writeHead(status, { "content-type": "application/json; charset=utf-8", "content-length": data.length });
    res.end(data);
  };
  const auth = (req: IncomingMessage) => {
    if (opts.apiKey && req.headers.authorization !== `Bearer ${opts.apiKey}`) throw new ApiError("unauthorized");
  };
  const waitOf = (url: URL) => {
    const w = url.searchParams.get("wait");
    if (w === null) return 0;
    const n = Number(w);
    if (!Number.isInteger(n) || n < 0 || n > 120) throw new ApiError("bad_request", "wait must be an integer 0..120");
    return n;
  };
  const readJson = (req: IncomingMessage) => new Promise<unknown>((resolve, reject) => {
    const chunks: Buffer[] = []; let size = 0;
    req.on("data", (c: Buffer) => { size += c.length; if (size > maxBody) { reject(new ApiError("media_too_large", `body > ${maxBody} bytes`)); req.destroy(); } else chunks.push(c); });
    req.on("end", () => { try { resolve(JSON.parse(Buffer.concat(chunks).toString("utf8"))); } catch (e) { reject(new ApiError("bad_request", `body is not JSON: ${(e as Error).message}`)); } });
    req.on("error", reject);
  });

  return (req, res) => {
    (async () => {
      const url = new URL(req.url ?? "/", "http://b");
      const m = url.pathname.match(/^\/jobs\/([^/]+)$/);
      if (req.method === "GET" && url.pathname === "/health") return reply(res, ...tuple(broker.health()));
      auth(req);
      if (req.method === "POST" && url.pathname === "/jobs") {
        const job = await readJson(req);
        if (!validateJob(job)) throw new ApiError("bad_request", errorsOf(validateJob).slice(0, 3).join("; "));
        return reply(res, ...tuple(await broker.submit(job as any)));
      }
      if (m && req.method === "GET") return reply(res, ...tuple(await broker.get(decodeURIComponent(m[1]), waitOf(url))));
      if (m && req.method === "DELETE") return reply(res, ...tuple(await broker.cancel(decodeURIComponent(m[1]), waitOf(url))));
      if (req.method === "GET" && url.pathname === "/capabilities") return reply(res, ...tuple(broker.capabilities()));
      throw new ApiError("not_found", `${req.method} ${url.pathname}`);
    })().catch((e) => {
      if (e instanceof ApiError) return reply(res, e.httpStatus, e.body());
      log(`http 500: ${(e as Error).stack ?? e}`);
      reply(res, 500, { error: "engine_failed", message: `broker: ${(e as Error).message}` });
    });
  };
}

const tuple = (r: { status: number; body: unknown }): [number, unknown] => [r.status, r.body];
