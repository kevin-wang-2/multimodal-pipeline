import type { CapabilitiesResponse, HealthResponse, JobDone, JobRequest } from "@mmp/protocol";
import { MmpError } from "./errors.js";
import { describeCapability, resolveCapability, type CapabilityQuery } from "./capabilities.js";

/** 任务态响应：200 done / cancelled、202 queued / running。失败以 MmpError 抛出。 */
export type JobStatusResponse = JobDone | {
  job_id: string; type: string; media_id?: string; status: "queued" | "running" | "cancelled";
  eta_sec?: number; queue_position?: number; cancel_requested?: boolean;
};

export interface ClientOptions {
  /** B 的任务 API 根地址，如 https://mmp.example.com */
  baseUrl: string;
  /** B 的 api_key（没设就不带） */
  apiKey?: string;
  /** 自定义 fetch（测试 / 特殊运行时） */
  fetch?: typeof fetch;
  /** 单个 HTTP 请求的超时，默认 wait + 15s，最少 20s */
  requestTimeoutMs?: (waitSec: number) => number;
  /** 传输层 / node_offline / backpressure 的自动重试次数（默认 2）与基础退避（默认 500ms，指数增长） */
  retries?: number;
  retryBaseMs?: number;
  /** 额外请求头 */
  headers?: Record<string, string>;
}

export interface WaitOptions {
  /** 总等待上限（默认 10 分钟） */
  timeoutMs?: number;
  /** 每次长轮询的 wait 秒数（默认 30，协议上限 120） */
  pollWaitSec?: number;
  signal?: AbortSignal;
}

export class MmpClient {
  private readonly base: string;
  private readonly fetchImpl: typeof fetch;
  private readonly o: Required<Pick<ClientOptions, "retries" | "retryBaseMs">> & ClientOptions;

  constructor(options: ClientOptions) {
    this.o = { retries: 2, retryBaseMs: 500, ...options };
    this.base = options.baseUrl.replace(/\/+$/, "");
    this.fetchImpl = options.fetch ?? globalThis.fetch;
    if (!this.fetchImpl) throw new Error("no fetch available; pass options.fetch");
  }

  // ---- 五个操作 ----
  submit(job: JobRequest): Promise<JobStatusResponse> {
    return this.request("POST", "/jobs", job, Number(job.wait ?? 0)) as Promise<JobStatusResponse>;
  }

  get(jobId: string, waitSec = 0): Promise<JobStatusResponse> {
    return this.request("GET", `/jobs/${encodeURIComponent(jobId)}${waitSec ? `?wait=${Math.trunc(waitSec)}` : ""}`, undefined, waitSec) as Promise<JobStatusResponse>;
  }

  cancel(jobId: string, waitSec = 0): Promise<JobStatusResponse> {
    return this.request("DELETE", `/jobs/${encodeURIComponent(jobId)}${waitSec ? `?wait=${Math.trunc(waitSec)}` : ""}`, undefined, waitSec) as Promise<JobStatusResponse>;
  }

  capabilities(): Promise<CapabilitiesResponse> {
    return this.request("GET", "/capabilities") as Promise<CapabilitiesResponse>;
  }

  async resolveCapability(query: CapabilityQuery) {
    return resolveCapability(await this.capabilities(), query);
  }

  /** 按 id 拉取一项能力的说明、输入 schema、档位与输出契约。 */
  async describe(id: string) {
    return describeCapability(await this.capabilities(), id);
  }

  health(): Promise<HealthResponse> {
    return this.request("GET", "/health") as Promise<HealthResponse>;
  }

  /** 轮询到 done。failed / cancelled 抛 MmpError（code 分别为任务的 error / "cancelled" 视作 unexpected）。 */
  async wait(jobId: string, opts: WaitOptions = {}): Promise<JobDone> {
    const deadline = Date.now() + (opts.timeoutMs ?? 600_000);
    const pollWait = Math.min(120, Math.max(1, opts.pollWaitSec ?? 30));
    for (;;) {
      opts.signal?.throwIfAborted();
      const remaining = deadline - Date.now();
      if (remaining <= 0) throw new MmpError("timeout", `job ${jobId} not done within ${opts.timeoutMs ?? 600_000}ms`, { jobId });
      const r = await this.get(jobId, Math.min(pollWait, Math.ceil(remaining / 1000)));
      if (r.status === "done") return r as JobDone;
      if (r.status === "cancelled") throw new MmpError("unexpected", `job ${jobId} was cancelled`, { jobId, body: r });
    }
  }

  /** 提交并等到结果。job.wait 缺省用 pollWaitSec，缓存命中 / 短任务一次往返就回。 */
  async run(job: JobRequest, opts: WaitOptions = {}): Promise<JobDone> {
    const first = await this.submit({ ...job, wait: job.wait ?? Math.min(120, opts.pollWaitSec ?? 30) });
    if (first.status === "done") return first as JobDone;
    if (first.status === "cancelled") throw new MmpError("unexpected", `job ${first.job_id} was cancelled`, { jobId: first.job_id, body: first });
    return this.wait(first.job_id, opts);
  }

  // ---- 传输 ----
  private async request(method: string, path: string, body?: unknown, waitSec = 0): Promise<unknown> {
    const timeoutMs = this.o.requestTimeoutMs ? this.o.requestTimeoutMs(waitSec) : Math.max(20_000, (waitSec + 15) * 1000);
    const headers: Record<string, string> = { accept: "application/json", ...(this.o.headers ?? {}) };
    if (this.o.apiKey) headers.authorization = `Bearer ${this.o.apiKey}`;
    if (body !== undefined) headers["content-type"] = "application/json";
    let attempt = 0;
    for (;;) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeoutMs);
      let res: Response;
      try {
        res = await this.fetchImpl(this.base + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal: ctrl.signal });
      } catch (e) {
        clearTimeout(timer);
        const err = (e as Error)?.name === "AbortError"
          ? new MmpError("timeout", `${method} ${path} timed out after ${timeoutMs}ms`, { cause: e })
          : new MmpError("network", `${method} ${path}: ${(e as Error)?.message ?? e}`, { cause: e });
        if (attempt < this.o.retries && err.retryable) { await this.backoff(attempt++); continue; }
        throw err;
      }
      clearTimeout(timer);
      const text = await res.text();
      let json: any;
      try { json = text ? JSON.parse(text) : {}; } catch { throw new MmpError("unexpected", `${method} ${path}: non-JSON response (HTTP ${res.status})`, { status: res.status, body: text }); }
      if (res.ok) return json;
      const err = MmpError.fromBody(res.status, json);
      // 原样重试只对"节点掉线 / 背压"有意义，且必须是幂等的读操作或还没入队的提交（错误体没有 job_id）
      if (attempt < this.o.retries && err.retryable && !err.jobId) {
        await this.backoff(attempt++, err.retryAfterSec);
        continue;
      }
      throw err;
    }
  }

  private backoff(attempt: number, retryAfterSec?: number): Promise<void> {
    const ms = retryAfterSec !== undefined ? retryAfterSec * 1000 : this.o.retryBaseMs * 2 ** attempt * (0.8 + Math.random() * 0.4);
    return new Promise((r) => setTimeout(r, Math.min(ms, 30_000)));
  }
}
