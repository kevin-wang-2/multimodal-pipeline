#!/usr/bin/env node
/**
 * 独立模式：`mmp-broker [broker.toml]`。任意键可用环境变量 MMP_BROKER__<KEY> 覆盖（如 MMP_BROKER__PORT=9000、MMP_BROKER__NODE_KEY=…）。
 */
import { readFileSync, existsSync } from "node:fs";
import { parse } from "smol-toml";
import { startBroker, type BrokerServerConfig } from "./index.js";

function loadConfig(path: string | undefined): BrokerServerConfig {
  let raw: Record<string, any> = {};
  if (path && existsSync(path)) raw = (parse(readFileSync(path, "utf8")) as any).broker ?? {};
  else if (path) console.error(`[mmp-broker] config ${path} not found, using env / defaults`);
  for (const [k, v] of Object.entries(process.env)) {
    if (k.startsWith("MMP_BROKER__") && v !== undefined) raw[k.slice("MMP_BROKER__".length).toLowerCase()] = /^\d+(\.\d+)?$/.test(v) ? Number(v) : v;
  }
  if (!raw.node_key || String(raw.node_key).length < 16) {
    console.error("[mmp-broker] broker.node_key (>=16 chars) is required; set it in broker.toml or MMP_BROKER__NODE_KEY");
    process.exit(2);
  }
  return {
    host: raw.host ?? "0.0.0.0", port: raw.port ?? 8766, wsPath: raw.ws_path ?? "/ws",
    nodeKey: String(raw.node_key), apiKey: raw.api_key ? String(raw.api_key) : undefined,
    inflightGraceSec: raw.inflight_grace_sec,
    heartbeatIntervalSec: raw.heartbeat_interval_sec, missedHeartbeats: raw.missed_heartbeats,
    registerTimeoutSec: raw.register_timeout_sec,
  };
}

const srv = await startBroker(loadConfig(process.argv[2]));
const stop = async () => { await srv.close(); process.exit(0); };
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
