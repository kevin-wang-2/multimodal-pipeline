/** B 行为契约：与 node/tests/test_broker_scenarios.py 跑同一批 protocol/fixtures/broker-scenarios。 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Broker, RegisterRejected } from "../src/core.js";
import { ApiError, type RequestPayload, type ResponsePayload } from "@mmp/protocol";

const DIR = resolve(dirname(fileURLToPath(import.meta.url)), "../../../../protocol/fixtures/broker-scenarios");

function subset(expected: any, actual: any, path = "$"): void {
  if (Array.isArray(expected)) {
    assert.ok(Array.isArray(actual) && actual.length === expected.length, `${path}: list length mismatch: ${JSON.stringify(actual)}`);
    expected.forEach((e, i) => subset(e, actual[i], `${path}[${i}]`));
  } else if (expected !== null && typeof expected === "object") {
    assert.ok(actual !== null && typeof actual === "object", `${path}: expected object, got ${JSON.stringify(actual)}`);
    for (const [k, v] of Object.entries(expected)) {
      assert.ok(k in actual, `${path}.${k} missing in ${JSON.stringify(actual)}`);
      subset(v, actual[k], `${path}.${k}`);
    }
  } else {
    assert.deepEqual(actual, expected, `${path}`);
  }
}

class FakeNode {
  received: RequestPayload[] = [];
  constructor(public nodeId: string, public replies: Record<string, { http_status: number; body: unknown }>, public delaySec: number) {}
  send = async (payload: RequestPayload): Promise<ResponsePayload> => {
    this.received.push(payload);
    if (this.delaySec) await new Promise((r) => setTimeout(r, this.delaySec * 1000));
    const rep = this.replies[payload.op];
    if (!rep) return { req_id: payload.req_id, http_status: 404, body: { error: "not_found" } } as ResponsePayload;
    const jobId = (payload as any).job_id ?? `${this.nodeId}-01J7ZQ9K3W8B6Q4M2N1P5R7S9T`;
    const body = JSON.parse(JSON.stringify(rep.body).replaceAll("$echo_job_id", jobId));
    return { req_id: payload.req_id, http_status: rep.http_status, body } as ResponsePayload;
  };
}

test("request timeout aborts transport pending without dropping the node", async () => {
  const broker = new Broker({ nodeKey: "scenario-node-key-0123456789", inflightGraceSec: 0.01 });
  let aborted = false;
  const send = (_payload: RequestPayload, signal?: AbortSignal): Promise<ResponsePayload> => new Promise((_resolve, reject) => {
    signal?.addEventListener("abort", () => {
      aborted = true;
      reject(new ApiError("timeout", "aborted"));
    }, { once: true });
  });
  broker.onRegister({
    type: "register", protocol_version: "1.1", ts: "2026-09-21T00:00:00Z",
    payload: { node_id: "node-a", node_key: "scenario-node-key-0123456789", capabilities: [{
      id: "echo", modal: "audio", tiers: [{ tier: "cpu", engine: "e-echo", engine_version: "1", cost: "low" }],
      input: { media: "required" }, output_schema: { type: "object" },
    }], engine_versions: { "e-echo": "1" } },
  }, send);

  const reply = await broker.submit({ type: "echo", media: { ref: "sha256:6b4a2b8c89e0bcf240920e7922d8d77666f7b61a11ba2ba7389f8156986220f1" } });
  assert.equal(reply.status, 504);
  assert.equal(reply.body.error, "timeout");
  assert.equal(aborted, true);
  assert.equal(broker.health().body.status, "ok");
});

for (const file of readdirSync(DIR).filter((f) => f.endsWith(".json")).sort()) {
  test(file.replace(/\.json$/, ""), async () => {
    const sc = JSON.parse(readFileSync(join(DIR, file), "utf8"));
    const clock = { t: 1_700_000_000 };
    const b = sc.broker ?? {};
    const broker = new Broker({ nodeKey: b.node_key, protocolVersion: b.protocol_version, inflightGraceSec: b.inflight_grace_sec,
      heartbeatIntervalSec: b.heartbeat_interval_sec, missedHeartbeats: b.missed_heartbeats, now: () => clock.t });
    const fakes = new Map<string, FakeNode>();
    for (const [i, step] of (sc.steps as any[]).entries()) {
      const where = `${file} step ${i}`;
      if ("register" in step) {
        const nodeId = step.register?.payload?.node_id ?? "?";
        const fake = new FakeNode(nodeId, step.replies ?? {}, step.delay_sec ?? 0);
        if ("expect_reject" in step) {
          assert.throws(() => broker.onRegister(step.register, fake.send), (e: any) => e instanceof RegisterRejected && e.code === step.expect_reject, where);
        } else {
          broker.onRegister(step.register, fake.send);
          fakes.set(nodeId, fake);
        }
      } else if ("heartbeat" in step) {
        broker.onHeartbeat(step.heartbeat);
      } else if ("disconnect" in step) {
        broker.onDisconnect(step.disconnect);
      } else if ("advance_sec" in step) {
        clock.t += step.advance_sec;
      } else if ("op" in step) {
        const before = new Map([...fakes].map(([n, f]) => [n, f.received.length]));
        const args = step.args ?? {};
        let r;
        switch (step.op) {
          case "submit": r = await broker.submit(args); break;
          case "get": r = await broker.get(args.job_id, args.wait ?? 0); break;
          case "cancel": r = await broker.cancel(args.job_id, args.wait ?? 0); break;
          case "capabilities": r = broker.capabilities(); break;
          case "health": r = broker.health(); break;
          default: throw new Error(`unknown op ${step.op}`);
        }
        const exp = step.expect ?? {};
        if ("http_status" in exp) assert.equal(r.status, exp.http_status, `${where}: body ${JSON.stringify(r.body)}`);
        if ("body" in exp) subset(exp.body, r.body, `${where} $`);
        if ("expect_routed_to" in step) {
          const routed = [...fakes].filter(([n, f]) => f.received.length > (before.get(n) ?? 0)).map(([n]) => n);
          assert.deepEqual(routed, step.expect_routed_to ? [step.expect_routed_to] : [], `${where}: routed`);
        }
      } else {
        throw new Error(`${where}: unknown step`);
      }
    }
  });
}
