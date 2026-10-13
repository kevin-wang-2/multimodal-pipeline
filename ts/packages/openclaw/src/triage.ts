import { MmpClient, MmpError, media } from "@mmp/client";
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
  if (!contentType) throw new MmpError("unsupported_type", "attachment Content-Type is unknown");
  const capability = await client.resolveCapability({ purpose: "triage", contentType });
  const mediaField = capability.input.fields.find((field) => field.kind === "media");
  if (!mediaField) throw new MmpError("unexpected", `${capability.id} has no media input`);
  const done = await client.run(
    { type: capability.id, arguments: { [mediaField.name]: media.inline(bytes, contentType) }, priority: "interactive" },
    { pollWaitSec: Math.min(120, cfg.waitSec ?? 50), timeoutMs: ((cfg.waitSec ?? 50) + 10) * 1000 },
  );
  if (!done.agent_context) throw new MmpError("unexpected", `${capability.id} completed without agent_context`, { jobId: done.job_id, body: done });
  const mediaId = done.media_ids[mediaField.name];
  if (typeof mediaId !== "string") throw new MmpError("unexpected", `${capability.id} returned no scalar media id`, { jobId: done.job_id, body: done });
  return { text: done.agent_context.text, mediaId, cached: done.cached };
}

export function makeClient(cfg: MmpHostConfig, fetchImpl?: typeof fetch): MmpClient {
  return new MmpClient({ baseUrl: cfg.baseUrl, apiKey: cfg.apiKey, fetch: fetchImpl, retries: 1 });
}
