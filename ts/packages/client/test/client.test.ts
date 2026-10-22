import { test } from "node:test";
import assert from "node:assert/strict";
import { describeCapability, matchesMediaType, MmpClient, MmpError, media, resolveCapability } from "../src/index.js";
import type { Usage } from "../src/index.js";

type Route = (req: { method: string; path: string; body: any; headers: Record<string, string> }) => { status: number; body?: unknown } | Promise<{ status: number; body?: unknown }>;

function fakeFetch(route: Route) {
  const calls: { method: string; path: string; body: any; headers: Record<string, string> }[] = [];
  const f = (async (url: string, init: RequestInit) => {
    const u = new URL(url);
    const body = init.body ? JSON.parse(init.body as string) : undefined;
    const req = { method: init.method ?? "GET", path: u.pathname + u.search, body, headers: init.headers as Record<string, string> };
    calls.push(req);
    const r = await route(req);
    return new Response(r.body === undefined ? "" : JSON.stringify(r.body), { status: r.status, headers: { "content-type": "application/json" } });
  }) as unknown as typeof fetch;
  return { f, calls };
}

const JOB = "n1-01J7ZQ9K3W8B6Q4M2N1P5R7S9T";
const MID = "sha256:" + "a".repeat(64);
const usage: Usage = { served_from: "compute", tier: "cpu", engine: "e", compute_ms: 12, wasted_ms: 0 };
const done = { job_id: JOB, type: "echo", status: "done", media_ids: { media: MID }, cached: false,
  source: { tier: "cpu", engine: "e", engine_version: "1", generated_at: "2026-09-21T00:00:00Z", degraded: false },
  usage, outputs: { result: { kind: "json", value: { ok: 1 } } } };

test("caller cancellation interrupts an in-flight poll without retrying", async () => {
  const controller = new AbortController();
  let calls = 0;
  let entered!: () => void;
  const started = new Promise<void>((resolve) => { entered = resolve; });
  const fetchImpl = (async (_url: unknown, init: RequestInit) => {
    calls++; entered();
    return new Promise<Response>((_resolve, reject) => init.signal!.addEventListener("abort", () => reject(init.signal!.reason), { once: true }));
  }) as typeof fetch;
  const client = new MmpClient({ baseUrl: "http://mmp", fetch: fetchImpl, retries: 3 });
  const waiting = client.wait(JOB, { signal: controller.signal });
  await started;
  const reason = new Error("caller stopped");
  controller.abort(reason);
  await assert.rejects(waiting, (error) => error === reason);
  assert.equal(calls, 1);
});

test("caller cancellation interrupts retry backoff and already-aborted submit makes no request", async () => {
  const controller = new AbortController();
  const { f, calls } = fakeFetch(() => ({ status: 503, body: { error: "node_offline" } }));
  const client = new MmpClient({ baseUrl: "http://mmp", fetch: f, retries: 5, retryBaseMs: 10_000 });
  const waiting = client.get(JOB, 0, controller.signal);
  setTimeout(() => controller.abort(new Error("stop during backoff")), 10);
  await assert.rejects(waiting, /stop during backoff/);
  await assert.rejects(client.submit({ type: "echo", arguments: { text: "x" } }, controller.signal), /stop during backoff/);
  assert.equal(calls.length, 1);
});

test("a failed submit response body never replays an already possibly accepted job", async () => {
  let calls = 0;
  const fetchImpl = (async () => {
    calls++;
    return new Response(new ReadableStream({ start(controller) { controller.error(new Error("response disconnected")); } }));
  }) as typeof fetch;
  const client = new MmpClient({ baseUrl: "http://mmp", fetch: fetchImpl, retries: 3 });
  await assert.rejects(client.submit({ type: "echo", arguments: { text: "x" } }), /response disconnected/);
  assert.equal(calls, 1);
});

test("submit sends auth + body and returns 200 body; media helpers build valid handles", async () => {
  const { f, calls } = fakeFetch(() => ({ status: 200, body: done }));
  const c = new MmpClient({ baseUrl: "https://b.example/", apiKey: "k", fetch: f });
  const r = await c.submit({ type: "echo", arguments: { media: media.inline(new Uint8Array([1, 2, 3]), "audio/wav") }, wait: 5 });
  assert.equal(r.status, "done");
  assert.deepEqual(r.usage, usage);
  assert.equal(calls[0].path, "/jobs");
  assert.equal(calls[0].headers.authorization, "Bearer k");
  assert.deepEqual(calls[0].body.arguments.media, { inline: "AQID", content_type: "audio/wav" });
  assert.deepEqual(media.ref(MID), { ref: MID });
  assert.deepEqual(media.refOr(MID, media.get("https://x/a")), { get: { url: "https://x/a" }, ref: MID });
});

test("errors map to MmpError with code / status / retry_after; JobFailed carries job_id", async () => {
  const { f } = fakeFetch((r) => r.path.startsWith("/jobs/") ? { status: 500, body: { job_id: JOB, type: "echo", status: "failed", error: "engine_failed", message: "boom" } }
    : { status: 429, body: { error: "backpressure", retry_after_sec: 0.01 } });
  const c = new MmpClient({ baseUrl: "https://b.example", fetch: f, retries: 1, retryBaseMs: 1 });
  await assert.rejects(c.submit({ type: "echo", arguments: { media: media.ref(MID) } }), (e: MmpError) => e instanceof MmpError && e.code === "backpressure" && e.status === 429 && e.retryAfterSec === 0.01 && e.retryable && e.shouldFallback);
  await assert.rejects(c.get(JOB), (e: MmpError) => e.code === "engine_failed" && e.jobId === JOB && !e.retryable && e.shouldFallback);
});

test("node_offline on a read is retried with backoff, then succeeds", async () => {
  let n = 0;
  const { f, calls } = fakeFetch(() => (++n < 3 ? { status: 503, body: { error: "node_offline" } } : { status: 200, body: done }));
  const c = new MmpClient({ baseUrl: "https://b.example", fetch: f, retries: 2, retryBaseMs: 1 });
  const r = await c.get(JOB, 5);
  assert.equal(r.status, "done");
  assert.equal(calls.length, 3);
  assert.equal(calls[0].path, `/jobs/${JOB}?wait=5`);
});

test("retries exhausted → throws the last error; no_node / bad_request are not retried", async () => {
  const { f, calls } = fakeFetch(() => ({ status: 503, body: { error: "no_node" } }));
  const c = new MmpClient({ baseUrl: "https://b.example", fetch: f, retries: 3, retryBaseMs: 1 });
  await assert.rejects(c.capabilities(), (e: MmpError) => e.code === "no_node");
  assert.equal(calls.length, 1, "no_node is not retryable (needs a node to come online, not a retry)");
  const bad = fakeFetch(() => ({ status: 400, body: { error: "bad_request", message: "x" } }));
  await assert.rejects(new MmpClient({ baseUrl: "https://b.example", fetch: bad.f }).submit({ type: "echo", arguments: { media: media.ref(MID) } }), (e: MmpError) => e.code === "bad_request");
  assert.equal(bad.calls.length, 1);
});

test("network failure is retried then surfaces as code=network", async () => {
  let n = 0;
  const f = (async () => { n++; throw new TypeError("fetch failed"); }) as unknown as typeof fetch;
  const c = new MmpClient({ baseUrl: "https://b.example", fetch: f, retries: 1, retryBaseMs: 1 });
  await assert.rejects(c.health(), (e: MmpError) => e.code === "network" && e.retryable && e.shouldFallback);
  assert.equal(n, 2);
});

test("run: 202 then polls until done; wait honours timeout and cancelled", async () => {
  let polls = 0;
  const { f, calls } = fakeFetch((r) => {
    if (r.method === "POST") return { status: 202, body: { job_id: JOB, type: "echo", media_ids: { media: MID }, status: "queued", queue_position: 0 } };
    return ++polls < 2 ? { status: 202, body: { job_id: JOB, type: "echo", media_ids: { media: MID }, status: "running" } } : { status: 200, body: done };
  });
  const c = new MmpClient({ baseUrl: "https://b.example", fetch: f });
  const r = await c.run({ type: "echo", arguments: { media: media.ref(MID) } }, { pollWaitSec: 7 });
  assert.equal(r.status, "done");
  assert.equal(calls[0].body.wait, 7);
  assert.equal(calls[1].path, `/jobs/${JOB}?wait=7`);
  const cancelled = fakeFetch(() => ({ status: 200, body: { job_id: JOB, type: "echo", media_ids: { media: MID }, status: "cancelled" } }));
  await assert.rejects(new MmpClient({ baseUrl: "https://b.example", fetch: cancelled.f }).wait(JOB), (e: MmpError) => /cancelled/.test(e.message));
  const slow = fakeFetch(() => ({ status: 202, body: { job_id: JOB, type: "echo", media_ids: { media: MID }, status: "running" } }));
  await assert.rejects(new MmpClient({ baseUrl: "https://b.example", fetch: slow.f }).wait(JOB, { timeoutMs: 1, pollWaitSec: 1 }), (e: MmpError) => e.code === "timeout");
});

test("capability resolution is generic across media families and fails explicitly", () => {
  const response = { protocol_version: "3.0", capabilities: [
    { capability: { id: "triage.audio", visibility: "public", purpose: "triage", tiers: [], input: { fields: [{ name: "media", kind: "media", required: true, accepts: ["audio/*", "video/*"], cardinality: { min: 1, max: 1 } }] }, output: { fields: [{ name: "digest", kind: "json", required: true, schema: {} }], agent_context: "required" } }, nodes: ["n1"] },
    { capability: { id: "triage.image", visibility: "public", purpose: "triage", tiers: [], input: { fields: [{ name: "image", kind: "media", required: true, accepts: ["image/*"], cardinality: { min: 1, max: 1 } }] }, output: { fields: [{ name: "digest", kind: "json", required: true, schema: {} }], agent_context: "required" } }, nodes: ["n1"] },
  ] } as any;
  assert.equal(matchesMediaType("image/*", "Image/PNG; charset=binary"), true);
  assert.equal(resolveCapability(response, { purpose: "triage", contentType: "video/mp4" }).id, "triage.audio");
  assert.equal(resolveCapability(response, { purpose: "triage", contentType: "image/png" }).id, "triage.image");
  assert.throws(() => resolveCapability(response, { purpose: "triage", contentType: "application/pdf" }),
    (e: MmpError) => e.code === "unsupported_type");
  assert.throws(() => resolveCapability(response, { purpose: "triage", contentType: "model/gltf+json" }),
    (e: MmpError) => e.code === "unsupported_type");
  assert.equal(describeCapability(response, "triage.image").input.fields[0].name, "image");
  assert.throws(() => describeCapability(response, "triage.video"),
    (e: MmpError) => e.code === "unsupported_type");
});

test("describe fetches only on demand and leaves existing client calls unchanged", async () => {
  const response = { protocol_version: "3.0", capabilities: [
    { capability: { id: "ocr.structured", visibility: "public", purpose: "ocr", description: "OCR", tiers: [],
      input: { fields: [
        { name: "media", kind: "media", required: true, accepts: ["image/*", "application/pdf"], cardinality: { min: 1, max: 1 } },
        { name: "options", kind: "json", required: false, schema: { type: "object", properties: { pages: { type: "array" } } } },
      ] }, output: { fields: [{ name: "document", kind: "json", required: true, schema: {} }], agent_context: "none" } }, nodes: ["n1"] },
  ] } as any;
  const { f, calls } = fakeFetch(() => ({ status: 200, body: response }));
  const c = new MmpClient({ baseUrl: "https://b.example", fetch: f });
  assert.equal((await c.describe("ocr.structured")).description, "OCR");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/capabilities");
  await assert.rejects(c.describe("missing"), (e: MmpError) => e.code === "unsupported_type");
});
