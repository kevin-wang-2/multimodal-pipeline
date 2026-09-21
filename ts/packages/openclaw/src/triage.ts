import { MmpClient, MmpError, media, renderDigest, type Digest } from "@mmp/client";
import type { MmpHostConfig } from "./config.js";

export interface TriageOutcome {
  /** 写到 stdout 的注入块 */
  text: string;
  mediaId: string;
  cached: boolean;
}

/** 一个附件 → 注入块。失败以 MmpError 抛出（CLI 据此决定退出码，让 OpenClaw 回落到下一个条目）。 */
export async function triageBytes(client: MmpClient, bytes: Uint8Array, contentType: string | undefined, cfg: MmpHostConfig): Promise<TriageOutcome> {
  if (bytes.byteLength > (cfg.maxInlineBytes ?? 8 * 1024 * 1024)) {
    throw new MmpError("media_too_large", `attachment is ${bytes.byteLength} bytes > inline limit; no public URL available from this host`);
  }
  const done = await client.run(
    { type: "triage.audio", media: media.inline(bytes, contentType), priority: "interactive" },
    { pollWaitSec: Math.min(120, cfg.waitSec ?? 50), timeoutMs: ((cfg.waitSec ?? 50) + 10) * 1000 },
  );
  return { text: renderDigest(done.result as unknown as Digest), mediaId: done.media_id, cached: done.cached };
}

export function makeClient(cfg: MmpHostConfig, fetchImpl?: typeof fetch): MmpClient {
  return new MmpClient({ baseUrl: cfg.baseUrl, apiKey: cfg.apiKey, fetch: fetchImpl, retries: 1 });
}
