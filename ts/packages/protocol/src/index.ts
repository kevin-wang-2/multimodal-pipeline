/**
 * 协议运行时：加载 protocol/schemas、ajv 校验器、错误码 → HTTP 状态。类型见 ./types（生成物）。
 */
import { Ajv2020, type ValidateFunction } from "ajv/dist/2020.js";
import addFormatsMod from "ajv-formats";

// ajv-formats 是 CJS，NodeNext 下 default 可能是模块命名空间；两种形态都接住
const addFormats: (ajv: Ajv2020, formats?: string[]) => unknown = ((addFormatsMod as any).default ?? addFormatsMod) as any;
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export * from "./types.js";

export const PROTOCOL_VERSION = "2.0";

/** schema 目录：发布的包里是 <pkg>/schemas（build 时从仓库 protocol/schemas 复制）；monorepo 内开发时回退到仓库路径。 */
function locateSchemas(): string {
  const here = dirname(fileURLToPath(import.meta.url));
  for (const cand of [resolve(here, "../schemas"), resolve(here, "../../../../protocol/schemas")]) {
    try { if (readdirSync(cand).some((f) => f.endsWith(".schema.json"))) return cand; } catch { /* next */ }
  }
  throw new Error("@mmp/protocol: schemas directory not found (package built without schemas?)");
}
export const SCHEMAS_DIR = locateSchemas();

export const ID = {
  abMessage: "urn:mmp:protocol:2:ab-message",
  jobApi: "urn:mmp:protocol:2:job-api",
  mediaHandle: "urn:mmp:protocol:2:media-handle",
  digest: "urn:mmp:protocol:2:digest",
  capability: "urn:mmp:protocol:2:capability",
  engineIo: "urn:mmp:protocol:2:engine-io",
} as const;

/** 协议.md §4：error → HTTP 状态。单向函数；C 只看 error。 */
export const HTTP_STATUS: Record<string, number> = {
  bad_request: 400, unsupported_type: 400, unauthorized: 401, not_found: 404, media_too_large: 413,
  media_fetch_failed: 422, media_not_found: 422, media_hash_mismatch: 422, backpressure: 429,
  engine_failed: 500, no_node: 503, node_offline: 503, timeout: 504,
};

export type ErrorCode = keyof typeof HTTP_STATUS;

let ajv: Ajv2020 | undefined;
export function schemas(): Ajv2020 {
  if (!ajv) {
    ajv = new Ajv2020({ strictSchema: true, strictTypes: false, strictRequired: false, allErrors: true, validateFormats: true });
    addFormats(ajv, ["date-time", "uri"]);
    for (const f of readdirSync(SCHEMAS_DIR).filter((n) => n.endsWith(".schema.json")).sort()) {
      ajv.addSchema(JSON.parse(readFileSync(join(SCHEMAS_DIR, f), "utf8")));
    }
  }
  return ajv;
}

const cache = new Map<string, ValidateFunction>();
/** ref 形如 "urn:mmp:protocol:2:job-api#/$defs/JobRequest" 或某个根 schema 的 $id。 */
export function validator(ref: string): ValidateFunction {
  let v = cache.get(ref);
  if (!v) {
    const a = schemas();
    v = (a.getSchema(ref) ?? a.compile({ $ref: ref })) as ValidateFunction;
    cache.set(ref, v);
  }
  return v;
}

export function errorsOf(v: ValidateFunction): string[] {
  return (v.errors ?? []).map((e) => `${e.instancePath || "$"}: ${e.message ?? ""}`);
}

export class ApiError extends Error {
  constructor(public code: ErrorCode, message?: string, public retryAfterSec?: number) {
    super(message ?? code);
  }
  get httpStatus(): number { return HTTP_STATUS[this.code]; }
  body(): { error: string; message?: string; retry_after_sec?: number } {
    const b: { error: string; message?: string; retry_after_sec?: number } = { error: this.code };
    if (this.message && this.message !== this.code) b.message = this.message;
    if (this.retryAfterSec !== undefined) b.retry_after_sec = this.retryAfterSec;
    return b;
  }
}
