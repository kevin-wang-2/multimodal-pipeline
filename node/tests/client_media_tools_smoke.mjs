import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const { MmpClient, media } = await import("../../ts/packages/client/dist/index.js");
const client = new MmpClient({ baseUrl: process.argv[2], retries: 0 });
const resize = await client.resolveCapability({ purpose: "image.resize", contentType: "image/png" });
assert.equal(resize.id, "image.resize");
const first = await client.run({ type: resize.id, arguments: {
  image: media.inline(readFileSync(process.argv[3]), "image/png"),
  options: { width: 6, height: 6 },
} });
assert.equal(first.outputs.metadata.value.width, 6);
assert.equal(first.outputs.metadata.value.height, 4);
const inspect = await client.run({ type: "image.inspect", arguments: {
  image: media.ref(first.outputs.image.media_id),
} });
assert.equal(inspect.outputs.metadata.value.width, 6);
const split = await client.run({ type: "audio.channels", arguments: {
  audio: media.inline(readFileSync(process.argv[4]), "audio/wav"),
  options: { mode: "split" },
} });
assert.equal(split.outputs.audio.length, 2);
assert.ok(split.outputs.audio.every((output) => !output.inline && !output.delivered));
const deliver = await client.resolveCapability({ purpose: "media.deliver", contentType: "audio/flac" });
const deliveryJob = { type: deliver.id, node_id: split.job_id.slice(0, -27),
  arguments: { media: split.outputs.audio.map((output) => media.ref(output.media_id)) },
  delivery: { media: split.outputs.audio.map((_, index) => ({ url: `${process.argv[5]}/channel-${index}.flac` })) },
};
const delivered = await client.run(deliveryJob);
assert.deepEqual(delivered.outputs.media.map((output) => output.media_id), split.outputs.audio.map((output) => output.media_id));
assert.ok(delivered.outputs.media.every((output) => output.delivered));
const request = { type: "audio.concat", arguments: {
  audio: split.outputs.audio.map((output) => media.ref(output.media_id)),
} };
const joined = await client.run(request);
assert.equal(joined.outputs.metadata.value.files[0].channels, 1);
assert.equal(joined.outputs.metadata.value.files[0].duration_sec, 1);
const cached = await client.run(request);
assert.equal(cached.cached, true);
assert.equal(cached.usage.compute_ms, 0);
await assert.rejects(client.run({ type: "image.convert", arguments: {
  image: media.inline(new TextEncoder().encode("not an image")),
} }), (error) => error.code === "bad_request" && error.message.includes("unsupported_format"));
process.stdout.write(JSON.stringify({ capabilities: 21, width: 6, split: 2, cached: true }));
