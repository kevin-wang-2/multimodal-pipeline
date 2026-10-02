import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const moduleUrl = process.argv[3]
  ? pathToFileURL(process.argv[3]).href
  : new URL("../../ts/packages/client/dist/index.js", import.meta.url).href;
const { MmpClient, media } = await import(moduleUrl);

const client = new MmpClient({ baseUrl: process.argv[2], retries: 0 });
const triage = await client.resolveCapability({ purpose: "triage", contentType: "image/png" });
const preflight = await client.run({
  type: triage.id,
  media: media.inline(new Uint8Array([137, 80, 78, 71]), "image/png"),
});

assert.deepEqual(preflight.result.capabilities_available, ["ocr.structured"]);
const ocr = typeof client.describe === "function"
  ? await client.describe("ocr.structured")
  : (await client.capabilities()).capabilities.find(({ capability }) => capability.id === "ocr.structured")?.capability;
assert.ok(ocr);
assert.equal(ocr.purpose, "ocr");
assert.ok(ocr.input.params_schema.properties.pages);

const result = await client.run({ type: ocr.id, media: media.ref(preflight.media_id) });
assert.equal(result.result.pages[0].text, "contract");
process.stdout.write(JSON.stringify({ capability: ocr.id, mediaId: preflight.media_id }));
