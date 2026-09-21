#!/usr/bin/env node
/**
 * mmp-triage <attachment-path> [--content-type audio/mp4]
 *   作为 OpenClaw tools.media.models[] 的 cli 条目：stdout = 注入块（成为 [Audio] 块的转写）。
 *   退出码：0 成功；2 服务侧不可用 / 媒体不合适（OpenClaw 会回落到下一个条目）；1 配置错误。
 * mmp-triage --check    连通性：health + capabilities
 */
import { readFileSync } from "node:fs";
import { extname } from "node:path";
import { MmpError } from "@mmp/client";
import { loadConfig } from "./config.js";
import { makeClient, triageBytes } from "./triage.js";

const MIME: Record<string, string> = { ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".aac": "audio/aac", ".mp3": "audio/mpeg", ".wav": "audio/wav",
  ".ogg": "audio/ogg", ".opus": "audio/ogg", ".flac": "audio/flac", ".amr": "audio/amr", ".webm": "audio/webm", ".aiff": "audio/aiff" };

async function main(argv: string[]): Promise<number> {
  const args = argv.slice(2);
  let cfg;
  try { cfg = loadConfig(); } catch (e) { console.error(String((e as Error).message)); return 1; }
  const client = makeClient(cfg);
  if (args[0] === "--check") {
    try {
      const h = await client.health();
      const caps = await client.capabilities();
      console.log(`health=${h.status} nodes=${h.nodes.map((n) => n.node_id).join(",") || "-"} capabilities=${caps.capabilities.map((c) => c.capability.id).join(",")}`);
      return h.status === "ok" ? 0 : 2;
    } catch (e) {
      console.error(`mmp-triage: ${e instanceof MmpError ? `${e.code}: ${e.message}` : String(e)}`);
      return 2;
    }
  }
  const path = args.find((a) => !a.startsWith("--"));
  if (!path) { console.error("usage: mmp-triage <attachment-path> [--content-type <mime>] | --check"); return 1; }
  const ctIdx = args.indexOf("--content-type");
  const contentType = ctIdx >= 0 ? args[ctIdx + 1] : MIME[extname(path).toLowerCase()];
  const t0 = Date.now();
  try {
    const out = await triageBytes(client, readFileSync(path), contentType, cfg);
    process.stdout.write(out.text + "\n");
    console.error(`mmp-triage: ok media=${out.mediaId} cached=${out.cached} ${Date.now() - t0}ms`);
    return 0;
  } catch (e) {
    if (e instanceof MmpError) {
      console.error(`mmp-triage: ${e.code}${e.status ? ` (HTTP ${e.status})` : ""}: ${e.message} — falling back to the next tools.media entry`);
      return 2;
    }
    console.error(`mmp-triage: ${(e as Error).stack ?? e}`);
    return 2;
  }
}

main(process.argv).then((code) => process.exit(code), (e) => { console.error(e); process.exit(2); });
