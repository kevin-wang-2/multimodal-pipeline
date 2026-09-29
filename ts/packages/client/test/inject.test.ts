import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { renderDigest, renderUnavailable } from "../src/index.js";
import type { Digest } from "@mmp/protocol";

const FIX = resolve(dirname(fileURLToPath(import.meta.url)), "../../../../protocol/fixtures/digest/root/valid");
const load = (n: string) => JSON.parse(readFileSync(join(FIX, n), "utf8")) as Digest;

test("M0 digest renders deterministically with boundary markers, timeline, gaps, capabilities", () => {
  const d = load("s2_m0_hum_then_speech.json");
  const out = renderDigest(d);
  assert.equal(out, renderDigest(d), "deterministic");
  const lines = out.split("\n");
  assert.equal(lines[0], "[mmp:digest start]");
  assert.equal(lines.at(-1), "[mmp:digest end]");
  assert.match(lines[1], /^media=sha256:[0-9a-f]{64} kind=audio duration=4\.39s engine=.* tier=cpu degraded=no$/);
  assert.match(out, /- \[0\.00, 1\.98\) 非语音：Music 0\.7\d, Synthesizer 0\.6\d/);
  assert.match(out, /- \[1\.98, 4\.39\) 语音：「帮我把这段的谱子扒出来。」 \(zh\)/);
  assert.match(out, /缺口：\n- .*只知类别、不知内容/);
  assert.match(out, /结构事实：非语音段位于时间轴前部/);
  assert.match(out, /可用能力：pitch_transcribe, chord_recognize/);
  assert.ok(!out.includes("工具状态"), "all tools ok → no tool line");
  assert.ok(!out.includes("⚠"));
});

test("image digest is not silently rendered as an audio timeline before #25", () => {
  assert.throws(() => renderDigest(load("image_scene.json")), /image digest rendering is not implemented/);
});

test("degraded digest gets an explicit warning and failed tools are listed; ASR failure is stated", () => {
  const d = load("speech_segment_asr_failed.json");
  const out = renderDigest(d);
  assert.match(out, /degraded=partial_failure/);
  assert.match(out, /⚠ 本次预检降级（partial_failure）/);
  assert.match(out, /语音：ASR 失败，内容未知/);
  assert.match(out, /工具状态：asr failed/);
});

test("unclassified segments and long timelines fold", () => {
  const d = load("m0_hum_then_speech.json");
  assert.match(renderDigest(d), /非语音：类别无法判定/);
  const many = { ...d, segments: Array.from({ length: 50 }, (_, i) => ({ start: i, end: i + 1, labels: [], label_status: "unclassified", asr: null })) } as unknown as Digest;
  const out = renderDigest(many, { maxSegments: 10 });
  assert.match(out, /省略 40 段/);
  assert.equal(out.split("\n").filter((l) => l.startsWith("- [")).length, 10);
});

test("low-confidence speech shows the tagger's evidence instead of a guessed cause", () => {
  const d = load("s2_m0_hum_then_speech.json");
  const seg = { start: 0.61, end: 1.09, labels: [{ tag: "Sound effect", score: 0.62 }], label_status: "ok", asr: { text: "Yeah.", lang: "en", confidence: 0 } };
  const out = renderDigest({ ...d, segments: [seg as any, ...d.segments] } as Digest);
  assert.match(out, /- \[0\.61, 1\.09\) 语音（低置信 0\.00，打标 Sound effect 0\.62）：「Yeah\.」 \(en\)/);
  assert.ok(!out.includes("喷麦"));
});

test("unavailable block names the reason and carries the fallback text", () => {
  const out = renderUnavailable("no_node", "平台转写的一整段文字");
  assert.equal(out.split("\n")[0], "[mmp:digest unavailable]");
  assert.match(out, /本次未做多模态预检（no_node）/);
  assert.match(out, /平台转写兜底/);
  assert.match(out, /平台转写的一整段文字/);
  assert.ok(renderUnavailable("timeout").endsWith("[mmp:digest end]"));
});
