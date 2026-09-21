/**
 * B-ts：作为库嵌入宿主（B/C 绑定）或独立进程（cli.ts）都从这里起。
 */
import { createServer, type Server } from "node:http";
import { WebSocketServer } from "ws";
import { Broker, type BrokerOptions } from "./core.js";
import { createHttpHandler, type HttpOptions } from "./http.js";
import { attachWs, type WsOptions } from "./ws.js";

export { Broker, RegisterRejected, nodeOf } from "./core.js";
export type { BrokerOptions, NodeLink, SendFn, Reply } from "./core.js";
export { createHttpHandler } from "./http.js";
export { attachWs, envelope } from "./ws.js";

export interface BrokerServerConfig extends BrokerOptions, HttpOptions, WsOptions {
  host?: string;
  port?: number;
  wsPath?: string;
}

export interface BrokerServer {
  broker: Broker;
  server: Server;
  wss: WebSocketServer;
  port: number;
  close(): Promise<void>;
}

/** 起一个进程：HTTP（任务 API）与 ws（A 连入）共用同一个监听端口——公网侧只开一个口。 */
export async function startBroker(cfg: BrokerServerConfig): Promise<BrokerServer> {
  const log = cfg.log ?? ((m: string) => console.error(`[mmp-broker] ${m}`));
  const broker = new Broker({ ...cfg, log });
  const server = createServer(createHttpHandler(broker, { apiKey: cfg.apiKey, maxBodyBytes: cfg.maxBodyBytes, log }));
  const wss = new WebSocketServer({ server, path: cfg.wsPath ?? "/ws", maxPayload: 32 * 1024 * 1024 });
  attachWs(broker, wss, { registerTimeoutSec: cfg.registerTimeoutSec, log });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(cfg.port ?? 8766, cfg.host ?? "127.0.0.1", () => resolve());
  });
  const addr = server.address();
  const port = typeof addr === "object" && addr ? addr.port : (cfg.port ?? 8766);
  log(`listening on ${cfg.host ?? "127.0.0.1"}:${port} (ws path ${cfg.wsPath ?? "/ws"})`);
  return {
    broker, server, wss, port,
    close: () => new Promise<void>((resolve) => {
      for (const c of wss.clients) c.terminate();
      wss.close(() => server.close(() => resolve()));
      server.closeAllConnections?.();
    }),
  };
}
