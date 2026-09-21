import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { MmpError } from "@mmp/client";
import { loadConfig } from "../src/config.js";
import { needsUnavailableNote, unavailableKind, unavailableNote } from "../src/hook.js";
import { makeClient, triageBytes } from "../src/triage.js";

test("hook: adds the downgrade note only when an [Audio] block lacks our marker", () => {
  assert.equal(needsUnavailableNote("[Audio]\nTranscript: hello"), true);
  assert.equal(needsUnavailableNote("[Audio 1/2]\n..."), true);
  assert.equal(needsUnavailableNote("[Video]\n..."), true);   // 语音 m4a 被 OpenClaw 按魔数判成 video/mp4
  assert.equal(needsUnavailableNote("[Audio]\n[mmp:digest start]\n..."), false);
  assert.equal(needsUnavailableNote("[Audio]\n[mmp:digest unavailable]\n..."), false);
  assert.equal(needsUnavailableNote("just text"), false);
  assert.equal(needsUnavailableNote("[Image]\n..."), false);
  assert.match(unavailableNote(), /平台兜底/);
  // 两级都失败：没有块，只剩附件引用 → 另一种说明
  assert.equal(unavailableKind("用户发来附件 media://inbound/voice---667924c1.mp4"), "no-transcript");
  assert.equal(unavailableKind("附件：voice.m4a 请处理"), "no-transcript");
  assert.equal(unavailableKind("[Video]\n（平台转写）"), "fallback-transcript");
  assert.equal(unavailableKind("[mmp:digest start]\n… voice.m4a"), null);
  assert.match(unavailableNote("no-transcript"), /平台转写也没有产出/);
});

test("config: env overrides file; file supplies defaults", () => {
  const dir = mkdtempSync(join(tmpdir(), "mmp-oc-"));
  const f = join(dir, "mmp.json");
  writeFileSync(f, JSON.stringify({ baseUrl: "http://file", apiKey: "k-file", waitSec: 20 }));
  const c = loadConfig({}, f);
  assert.deepEqual([c.baseUrl, c.apiKey, c.waitSec, c.maxInlineBytes], ["http://file", "k-file", 20, 8 * 1024 * 1024]);
  const e = loadConfig({ MMP_BASE_URL: "http://env", MMP_API_KEY: "k-env" }, f);
  assert.deepEqual([e.baseUrl, e.apiKey], ["http://env", "k-env"]);
  assert.throws(() => loadConfig({}, join(dir, "missing.json")), /no baseUrl/);
});

const MID = "sha256:" + "b".repeat(64);
const digest = { media_id: MID, kind: "audio", duration_sec: 4.4, timeline_unit: "sec",
  segments: [{ start: 0, end: 2, labels: [{ tag: "Whistling", score: 0.95 }], label_status: "ok", asr: null },
             { start: 2, end: 4.4, labels: [{ tag: "Speech", score: 0.99 }], label_status: "ok", asr: { text: "扒谱", lang: "zh", confidence: 0.99 } }],
  tools: { vad: "ok", audio_tagging: "ok", asr: "ok" }, gaps: ["结构事实：非语音段位于时间轴前部、语音段在后（指令在后）"],
  capabilities_available: ["pitch_transcribe"], source: { tier: "cpu", engine: "e", engine_version: "1", generated_at: "2026-09-21T00:00:00Z", degraded: false } };

function fakeFetch(status: number, body: unknown) {
  const calls: any[] = [];
  const f = (async (url: string, init: RequestInit) => {
    calls.push({ url, body: init.body ? JSON.parse(init.body as string) : undefined });
    return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  }) as unknown as typeof fetch;
  return { f, calls };
}

test("triage: inline submit → injection block with marker; over-limit → media_too_large without a request", async () => {
  const done = { job_id: "n-01J7ZQ9K3W8B6Q4M2N1P5R7S9T", type: "triage.audio", status: "done", media_id: MID, cached: false, source: digest.source, result: digest };
  const { f, calls } = fakeFetch(200, done);
  const cfg = { baseUrl: "http://b", apiKey: "k", waitSec: 30, maxInlineBytes: 100 };
  const out = await triageBytes(makeClient(cfg, f), new Uint8Array(50), "audio/mp4", cfg);
  assert.ok(out.text.startsWith("[mmp:digest start]") && out.text.endsWith("[mmp:digest end]"));
  assert.match(out.text, /Whistling 0\.95/);
  assert.equal(out.mediaId, MID);
  assert.equal(calls[0].body.media.content_type, "audio/mp4");
  assert.equal(calls[0].body.priority, "interactive");
  await assert.rejects(triageBytes(makeClient(cfg, f), new Uint8Array(200), "audio/mp4", cfg), (e: MmpError) => e.code === "media_too_large");
  assert.equal(calls.length, 1);
});

test("triage: service errors surface as MmpError so the CLI can exit 2 (OpenClaw falls back)", async () => {
  const { f } = fakeFetch(503, { error: "no_node" });
  const cfg = { baseUrl: "http://b", waitSec: 5 };
  await assert.rejects(triageBytes(makeClient(cfg, f), new Uint8Array(10), "audio/wav", cfg), (e: MmpError) => e.code === "no_node" && e.shouldFallback);
});
