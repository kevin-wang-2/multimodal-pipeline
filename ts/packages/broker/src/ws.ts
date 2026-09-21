/**
 * A 连入的 ws 端点（协议.md §2）。这是系统里唯一的公网监听，放在 B 这一侧。
 * 每条连接：首条必须是 register（否则 4003），之后 heartbeat / response；B → A 只发 request。
 */
import { WebSocketServer, WebSocket } from "ws";
import { ApiError, ID, validator, type RequestPayload, type ResponsePayload } from "@mmp/protocol";
import { Broker, RegisterRejected, type NodeLink, type SendFn } from "./core.js";

export interface WsOptions {
  registerTimeoutSec?: number;   // 连接后多久内必须 register
  log?: (msg: string) => void;
}

export function envelope(broker: Broker, type: string, payload: unknown) {
  return { type, protocol_version: broker.protocolVersion, ts: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"), payload };
}

export function attachWs(broker: Broker, wss: WebSocketServer, opts: WsOptions = {}): void {
  const log = opts.log ?? (() => {});
  const validateEnv = validator(ID.abMessage);

  wss.on("connection", (ws: WebSocket, req) => {
    const peer = `${req.socket.remoteAddress}:${req.socket.remotePort}`;
    let link: NodeLink | undefined;
    const pending = new Map<string, { resolve: (r: ResponsePayload) => void; reject: (e: Error) => void }>();

    const registerTimer = setTimeout(() => {
      if (!link) { log(`${peer}: no register within ${opts.registerTimeoutSec ?? 10}s, closing`); ws.close(4003, "register timeout"); }
    }, (opts.registerTimeoutSec ?? 10) * 1000);

    const send: SendFn = (payload: RequestPayload) => new Promise((resolve, reject) => {
      if (ws.readyState !== WebSocket.OPEN) return reject(new ApiError("node_offline", "node connection is not open"));
      pending.set(payload.req_id, { resolve, reject });
      ws.send(JSON.stringify(envelope(broker, "request", payload)), (err) => {
        if (err) { pending.delete(payload.req_id); reject(new ApiError("node_offline", `send failed: ${err.message}`)); }
      });
    });

    ws.on("message", (data) => {
      let parsed: unknown;
      try { parsed = JSON.parse(data.toString("utf8")); } catch { if (!link) ws.close(4003, "not json"); else log(`${peer}: non-json frame ignored`); return; }
      const ok = validateEnv(parsed);
      const env = parsed as any;
      if (!ok) {
        if (!link) ws.close(4003, "register does not match schema");
        else log(`${peer}/${link.nodeId}: frame violates ab-message schema, ignored`);
        return;
      }
      if (!link || env.type === "register") {
        try {
          link = broker.onRegister(env, send);
          clearTimeout(registerTimer);
          log(`${peer}: node ${link.nodeId} registered (${link.capabilities.map((c) => c.id).join(",")})`);
        } catch (e) {
          const code = e instanceof RegisterRejected ? e.code : 4003;
          log(`${peer}: register rejected ${code} ${(e as Error).message}`);
          ws.close(code, (e as Error).message.slice(0, 120));
        }
        return;
      }
      if (env.type === "heartbeat") broker.onHeartbeat(env);
      else if (env.type === "response") {
        const p = pending.get(env.payload.req_id);
        if (p) { pending.delete(env.payload.req_id); p.resolve(env.payload); }
      }
    });

    const teardown = (why: string) => {
      clearTimeout(registerTimer);
      // 同一 node_id 若已被新连接覆盖，旧连接关闭不能把新表项删掉
      if (link && broker.nodes.get(link.nodeId) === link) broker.onDisconnect(link.nodeId);
      for (const p of pending.values()) p.reject(new ApiError("node_offline", `node disconnected (${why})`));
      pending.clear();
      if (link) log(`${peer}: node ${link.nodeId} gone (${why})`);
    };
    ws.on("close", (code, reason) => teardown(`close ${code} ${reason.toString()}`));
    ws.on("error", (e) => teardown(`error ${e.message}`));

    // Broker 因在途超时把节点丢掉时，连接视为不健康，关掉让 A 重连
    broker.onDropped((nodeId, dropped) => {
      if (dropped === link && ws.readyState === WebSocket.OPEN) ws.close(1011, "unresponsive");
    });
  });
}
