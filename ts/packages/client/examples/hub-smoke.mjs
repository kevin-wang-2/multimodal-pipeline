// 用打包后的 @mmp/client 打一个真实的 B：echo → triage.audio（inline）→ 同一媒体用 ref 重提 → 渲染注入块。
// 用法：MMP_BASE_URL=https://mmp.example MMP_API_KEY=… node examples/hub-smoke.mjs [wav]
import { readFileSync } from "node:fs";
import { MmpClient, MmpError, media, renderDigest, renderUnavailable } from "../dist/index.js";

const c = new MmpClient({ baseUrl: process.env.MMP_BASE_URL, apiKey: process.env.MMP_API_KEY });
const wav = process.argv[2] ?? new URL("../../../../testdata/m0_hum_then_speech.wav", import.meta.url).pathname;

const t = (ms) => `${ms} ms`;
try {
  const h = await c.health();
  console.log("health:", h.status, h.nodes.map((n) => n.node_id));
  const caps = await c.capabilities();
  console.log("capabilities:", caps.capabilities.map((x) => x.capability.id));

  let t0 = Date.now();
  const e = await c.run({ type: "echo", media: media.inline(new TextEncoder().encode("hello mmp")), params: { tag: "client-smoke" } });
  console.log(`echo: ${e.status} cached=${e.cached} ${t(Date.now() - t0)}`);

  t0 = Date.now();
  const d1 = await c.run({ type: "triage.audio", media: media.inline(readFileSync(wav), "audio/wav") }, { pollWaitSec: 60 });
  console.log(`triage.audio (inline): cached=${d1.cached} fetch=${d1.timings_ms?.fetch}ms ${t(Date.now() - t0)}`);

  t0 = Date.now();
  const d2 = await c.run({ type: "triage.audio", media: media.ref(d1.media_id), params: { label_confidence_threshold: 0.35 } }, { pollWaitSec: 60 });
  console.log(`triage.audio (ref, new params): cached=${d2.cached} fetch=${d2.timings_ms?.fetch}ms ${t(Date.now() - t0)}`);

  console.log("\n" + renderDigest(d1.result));
} catch (err) {
  if (err instanceof MmpError) {
    console.log(`MmpError ${err.code} (HTTP ${err.status ?? "-"}) retryable=${err.retryable} fallback=${err.shouldFallback}`);
    if (err.shouldFallback) console.log("\n" + renderUnavailable(err.code, "（这里放平台自己的转写）"));
    process.exitCode = 1;
  } else {
    throw err;
  }
}
