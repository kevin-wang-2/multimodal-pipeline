import type { ErrorCode } from "@mmp/protocol";

/** 客户端侧的错误分类：协议错误码（协议.md §4）+ 三个传输层原因。 */
export type ClientErrorCode = ErrorCode | "network" | "timeout" | "unexpected";

const RETRYABLE: ReadonlySet<string> = new Set(["node_offline", "backpressure", "network", "timeout"]);
const FALLBACK: ReadonlySet<string> = new Set(["no_node", "node_offline", "timeout", "network", "engine_failed", "backpressure"]);

export class MmpError extends Error {
  readonly code: ClientErrorCode;
  readonly status?: number;
  readonly retryAfterSec?: number;
  readonly jobId?: string;
  readonly body?: unknown;

  constructor(code: ClientErrorCode, message: string, extra: { status?: number; retryAfterSec?: number; jobId?: string; body?: unknown; cause?: unknown } = {}) {
    super(message, extra.cause !== undefined ? { cause: extra.cause } : undefined);
    this.name = "MmpError";
    this.code = code;
    this.status = extra.status;
    this.retryAfterSec = extra.retryAfterSec;
    this.jobId = extra.jobId;
    this.body = extra.body;
  }

  /** 原样重试有意义（节点掉线 / 背压 / 网络抖动）。 */
  get retryable(): boolean { return RETRYABLE.has(this.code); }
  /** 服务侧不可用的一类：调用方该走自己的兜底（铁律 5），而不是修请求。 */
  get shouldFallback(): boolean { return FALLBACK.has(this.code); }

  static fromBody(status: number, body: any): MmpError {
    const code = (body && typeof body.error === "string" ? body.error : "unexpected") as ClientErrorCode;
    const msg = body?.message ? `${code}: ${body.message}` : `${code} (HTTP ${status})`;
    return new MmpError(code, msg, { status, retryAfterSec: body?.retry_after_sec, jobId: body?.job_id, body });
  }
}
