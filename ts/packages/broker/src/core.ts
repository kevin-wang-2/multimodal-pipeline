/**
 * Broker 操作层（协议.md §0.1）：submit / get / cancel / capabilities / health → { status, body }。
 * 与 node/mmp_broker/core.py 一一对应；两者跑同一批 protocol/fixtures/broker-scenarios。
 * 无状态：只有已连接节点、能力表（在节点上）、在途请求三张内存表。
 */
import { ApiError, type Capability, type HeartbeatPayload, type JobRequest, type RequestPayload, type ResponsePayload } from "@mmp/protocol";

export type SendFn = (payload: RequestPayload) => Promise<ResponsePayload>;
export type Reply = { status: number; body: Record<string, unknown> };

export class RegisterRejected extends Error {
  constructor(public code: 4001 | 4002 | 4003, public reason: string) {
    super(`${code} ${reason}`);
  }
}

export interface NodeLink {
  nodeId: string;
  send: SendFn;
  capabilities: Capability[];
  engineVersions: Record<string, string>;
  connectedSince: number;
  lastHeartbeat?: number;
  heartbeat?: HeartbeatPayload;
}

export interface BrokerOptions {
  nodeKey: string;
  protocolVersion?: string;
  inflightGraceSec?: number;
  heartbeatIntervalSec?: number;
  missedHeartbeats?: number;
  now?: () => number;                 // 秒；可注入，契约场景用它拨快心跳超时
  log?: (msg: string) => void;
}

/** job_id = <node_id>-<ULID>：从右边切 26 位 ULID 取前缀。 */
export function nodeOf(jobId: string): string | null {
  if (jobId.length < 28 || jobId[jobId.length - 27] !== "-") return null;
  return jobId.slice(0, -27);
}

const iso = (sec: number) => new Date(sec * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");

export class Broker {
  readonly nodeKey: string;
  readonly protocolVersion: string;
  readonly inflightGraceSec: number;
  readonly offlineAfter: number;
  readonly now: () => number;
  readonly nodes = new Map<string, NodeLink>();
  private reqSeq = 0;
  private log: (msg: string) => void;
  private dropHandlers: Array<(nodeId: string, link: NodeLink) => void> = [];

  constructor(o: BrokerOptions) {
    this.nodeKey = o.nodeKey;
    this.protocolVersion = o.protocolVersion ?? "1.1";
    this.inflightGraceSec = o.inflightGraceSec ?? 5;
    this.offlineAfter = (o.heartbeatIntervalSec ?? 10) * (o.missedHeartbeats ?? 3);
    this.now = o.now ?? (() => Date.now() / 1000);
    this.log = o.log ?? (() => {});
  }

  // ---- A 侧消息进入 ----
  onRegister(envelope: any, send: SendFn): NodeLink {
    if (envelope?.type !== "register") throw new RegisterRejected(4003, "first message must be register");
    const major = String(envelope.protocol_version ?? "").split(".")[0];
    if (major !== this.protocolVersion.split(".")[0]) {
      throw new RegisterRejected(4002, `protocol ${envelope.protocol_version} incompatible with ${this.protocolVersion}`);
    }
    const p = envelope.payload ?? {};
    if (p.node_key !== this.nodeKey) throw new RegisterRejected(4001, "bad node_key");
    if (!p.node_id || !Array.isArray(p.capabilities) || p.capabilities.length === 0) {
      throw new RegisterRejected(4003, "register missing node_id or capabilities");
    }
    const link: NodeLink = { nodeId: p.node_id, send, capabilities: p.capabilities, engineVersions: p.engine_versions ?? {}, connectedSince: this.now() };
    if (this.nodes.has(link.nodeId)) this.log(`node ${link.nodeId} re-registered, replacing old link`);
    this.nodes.set(link.nodeId, link);
    return link;
  }

  onHeartbeat(envelope: any): void {
    const p = envelope?.payload ?? {};
    const link = this.nodes.get(p.node_id);
    if (!link) return;
    link.lastHeartbeat = this.now();
    link.heartbeat = p;
  }

  onDisconnect(nodeId: string): void {
    this.nodes.delete(nodeId);
  }

  /** Broker 主动丢弃节点（在途超时 / 心跳超时）时通知传输层关连接。 */
  onDropped(fn: (nodeId: string, link: NodeLink) => void): void {
    this.dropHandlers.push(fn);
  }

  private drop(nodeId: string, link: NodeLink): void {
    this.nodes.delete(nodeId);
    for (const fn of this.dropHandlers) fn(nodeId, link);
  }

  private live(): NodeLink[] {
    const now = this.now();
    for (const [id, l] of [...this.nodes]) {
      if (l.lastHeartbeat !== undefined && now - l.lastHeartbeat > this.offlineAfter) {
        this.log(`node ${id} missed heartbeats, dropping`);
        this.drop(id, l);
      }
    }
    return [...this.nodes.values()];
  }

  // ---- 路由 ----
  private pick(taskType: string): NodeLink {
    const cands = this.live().filter((l) => l.capabilities.some((c) => c.id === taskType));
    if (cands.length === 0) throw new ApiError("no_node", `no online node offers '${taskType}'`);
    return cands.reduce((a, b) => ((b.heartbeat?.queue_len ?? 0) < (a.heartbeat?.queue_len ?? 0) ? b : a));
  }

  private byJob(jobId: string): NodeLink {
    const nid = nodeOf(jobId);
    if (nid === null) throw new ApiError("bad_request", `malformed job_id '${jobId}'`);
    const link = this.nodes.get(nid);
    if (!link || !this.live().includes(link)) throw new ApiError("node_offline", `node ${nid} is not connected`);
    return link;
  }

  private async call(link: NodeLink, payload: Omit<RequestPayload, "req_id">, waitSec: number): Promise<Reply> {
    const req = { req_id: `b${++this.reqSeq}`, ...payload } as RequestPayload;
    const timeoutMs = (waitSec + this.inflightGraceSec) * 1000;
    let timer: NodeJS.Timeout | undefined;
    const timeout = new Promise<never>((_, rej) => { timer = setTimeout(() => rej(new ApiError("node_offline", `node ${link.nodeId} did not respond in time`)), timeoutMs); });
    try {
      const resp = await Promise.race([link.send(req), timeout]);
      return { status: resp.http_status, body: resp.body as Record<string, unknown> };
    } catch (e) {
      if (e instanceof ApiError && e.code === "node_offline") {
        this.log(`node ${link.nodeId} did not answer ${payload.op} within ${timeoutMs}ms, dropping link`);
        if (this.nodes.get(link.nodeId) === link) this.drop(link.nodeId, link);
      }
      throw e;
    } finally {
      clearTimeout(timer);
    }
  }

  private static fail(e: unknown): Reply {
    if (e instanceof ApiError) return { status: e.httpStatus, body: e.body() };
    throw e;
  }

  // ---- 五个操作 ----
  async submit(job: JobRequest): Promise<Reply> {
    try {
      const link = this.pick(job.type);
      return await this.call(link, { op: "submit", job }, Number(job.wait ?? 0));
    } catch (e) { return Broker.fail(e); }
  }

  async get(jobId: string, wait = 0): Promise<Reply> {
    try {
      const link = this.byJob(jobId);
      return await this.call(link, { op: "get", job_id: jobId, wait: Math.trunc(wait) }, wait);
    } catch (e) { return Broker.fail(e); }
  }

  async cancel(jobId: string, wait = 0): Promise<Reply> {
    try {
      const link = this.byJob(jobId);
      return await this.call(link, { op: "cancel", job_id: jobId, wait: Math.trunc(wait) }, wait);
    } catch (e) { return Broker.fail(e); }
  }

  capabilities(): Reply {
    const merged = new Map<string, { capability: Capability; nodes: string[] }>();
    for (const l of this.live()) {
      for (const cap of l.capabilities) {
        const entry = merged.get(cap.id) ?? { capability: cap, nodes: [] };
        entry.nodes.push(l.nodeId);
        merged.set(cap.id, entry);
      }
    }
    return { status: 200, body: { protocol_version: this.protocolVersion, capabilities: [...merged.values()] } };
  }

  health(): Reply {
    const live = this.live();
    const nodes = live.map((l) => {
      const n: Record<string, unknown> = { node_id: l.nodeId, connected_since: iso(l.connectedSince), last_heartbeat: iso(l.lastHeartbeat ?? l.connectedSince) };
      if (l.heartbeat) n.heartbeat = l.heartbeat;
      return n;
    });
    return { status: 200, body: { status: live.length ? "ok" : "no_node", protocol_version: this.protocolVersion, nodes } };
  }
}
