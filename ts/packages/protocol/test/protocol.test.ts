import { test } from "node:test";
import assert from "node:assert/strict";
import { ApiError, HTTP_STATUS, ID, validator, errorsOf } from "../src/index.js";

test("validators resolve urn refs", () => {
  const v = validator(`${ID.jobApi}#/$defs/JobRequest`);
  assert.equal(v({ type: "echo", media: { ref: "sha256:" + "a".repeat(64) } }), true);
  assert.equal(v({ type: "echo", media: { path: "/x" } }), false);
  assert.ok(errorsOf(v).length > 0);
  assert.equal(validator(ID.abMessage)({ type: "heartbeat", protocol_version: "1.2", ts: "2026-09-21T00:00:00Z",
    payload: { node_id: "n", queue_len: 0, running: 0, engines_loaded: [] } }), true);
});

test("error codes cover the schema enum exactly", async () => {
  const { readFileSync } = await import("node:fs");
  const { join } = await import("node:path");
  const { SCHEMAS_DIR } = await import("../src/index.js");
  const s = JSON.parse(readFileSync(join(SCHEMAS_DIR, "job-api.schema.json"), "utf8"));
  assert.deepEqual(Object.keys(HTTP_STATUS).sort(), [...s.$defs.ErrorCode.enum].sort());
  assert.equal(new ApiError("backpressure", "full", 5).httpStatus, 429);
  assert.deepEqual(new ApiError("no_node").body(), { error: "no_node" });
});
