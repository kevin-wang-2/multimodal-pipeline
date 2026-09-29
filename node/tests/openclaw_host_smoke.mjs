import assert from "node:assert/strict";
import { makeClient, triageBytes } from "../../ts/packages/openclaw/dist/triage.js";

const baseUrl = process.argv[2];
const cfg = { baseUrl, waitSec: 10, maxInlineBytes: 1024 };
const outcome = await triageBytes(makeClient(cfg), new Uint8Array([137, 80, 78, 71]), "image/png", cfg);

assert.match(outcome.text, /^\[mmp:agent-context start\]/);
assert.match(outcome.text, /kind=image/);
assert.match(outcome.text, /surface=image_0/);
assert.match(outcome.text, /provenance=model_inference/);
assert.equal(outcome.cached, false);
process.stdout.write(JSON.stringify(outcome));
