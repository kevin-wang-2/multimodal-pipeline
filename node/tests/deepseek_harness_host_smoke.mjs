import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile, stat, open } from "node:fs/promises";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

// 通过插件自己的依赖解析真实 SDK，不要求仓库根目录有 npm workspace。
const require = createRequire(new URL("../../ts/packages/deepseek-harness/package.json", import.meta.url));
const { Context } = await import(pathToFileURL(require.resolve("@deepseek-ai/cordis")).href);
const { default: Tools } = await import(pathToFileURL(require.resolve("@deepseek-ai/dsh-tools")).href);
const { default: SystemPrompt } = await import(pathToFileURL(require.resolve("@deepseek-ai/dsh-system-prompt")).href);
const { ToolCallId } = await import(pathToFileURL(require.resolve("@deepseek-ai/dsh-llm")).href);
const plugin = await import("../../ts/packages/deepseek-harness/dist/index.js");
const [baseUrl, imagePath, audioPath, largePath, dataDir] = process.argv.slice(2);
const objects = new Map();
const attachments = {
  async saveFileStream({ data, name, signal }) {
    const chunks = [];
    for await (const part of data) { signal?.throwIfAborted(); chunks.push(part); }
    const bytes = Buffer.concat(chunks);
    const attachmentId = createHash("sha256").update(bytes).digest("hex");
    objects.set(attachmentId, bytes);
    return { attachmentId, bytes: bytes.length, name };
  },
  async *readFileStream(ref, signal) { signal?.throwIfAborted(); yield objects.get(ref.attachmentId); },
};
const ctx = new Context();
let mediaRoute;
try {
  await ctx.plugin(SystemPrompt);
  await ctx.plugin(Tools);
  ctx.provide("attachments", attachments);
  ctx.provide("agents", {});
  ctx.provide("fs", {
    async resolve(path) { assert.ok([imagePath, audioPath, largePath].includes(path)); return { path }; },
    async stat(target) { const info = await stat(target.path); return { type: "file", size: info.size }; },
    async readByteRange(target, range) {
      const file = await open(target.path);
      try {
        const data = Buffer.alloc(range.length);
        const { bytesRead } = await file.read(data, 0, range.length, range.offset);
        return data.subarray(0, bytesRead);
      } finally { await file.close(); }
    },
  });
  ctx.provide("connection", { fetch: { register(route) { mediaRoute = route; return async () => {}; } } });
  await ctx.plugin(plugin, { baseUrl, apiKey: "harness-integration-key", transferHost: "127.0.0.1", transferPort: 0,
    transferBaseUrl: "http://127.0.0.1", dataDir, timeoutMs: 60_000 });
  const agent = { id: "host-smoke", ctx, session: { id: "host-smoke", header: { cwd: dataDir }, deriveMessages: () => [] } };
  let serial = 0;
  const tool = async (name, args) => {
    const result = await ctx.tools.execute({ name, arguments: args, callId: ToolCallId(`smoke-${serial++}`), agent, signal: new AbortController().signal });
    assert.equal(result.isError, false, JSON.stringify(result.content));
    return result.value;
  };
  const list = await tool("mmp_list", {});
  assert.ok(list.capabilities.some((cap) => cap.id === "image.resize"));
  await tool("mmp_describe", { id: "image.resize" });
  const resized = await tool("mmp_run", { type: "image.resize", arguments: { image: { file_path: imagePath }, options: { width: 6, height: 6 } } });
  assert.equal(resized.job.outputs.metadata.value.width, 6);
  assert.equal(resized.assets[0].contentType, "image/png");
  const inspected = await tool("mmp_run", { type: "image.inspect", arguments: { image: { asset_id: resized.assets[0].id } } });
  assert.equal(inspected.job.outputs.metadata.value.width, 6);
  const split = await tool("mmp_run", { type: "audio.channels", arguments: { audio: { file_path: audioPath }, options: { mode: "split" } } });
  assert.equal(split.assets.length, 2);
  const joined = await tool("mmp_run", { type: "audio.concat", arguments: { audio: split.assets.map((asset) => ({ asset_id: asset.id })) } });
  assert.equal(joined.assets.length, 1);
  const delivered = await tool("mmp_run", { type: "media.deliver", node_id: split.node_id,
    arguments: { media: split.job.outputs.audio.map((output) => ({ ref: output.media_id })) } });
  assert.equal(delivered.assets.length, 2);
  const large = await tool("mmp_run", { type: "audio.inspect", arguments: { audio: { file_path: largePath } } });
  assert.ok(large.job.outputs.metadata.value.files[0].duration_sec > 100);
  const downloaded = await mediaRoute.fetch(new Request(`http://host${resized.assets[0].href}`));
  const outputBytes = Buffer.from(await downloaded.arrayBuffer());
  assert.ok(outputBytes.subarray(1, 4).equals(Buffer.from("PNG")));
  assert.ok((await readFile(largePath)).length > 8 * 1024 * 1024);
  process.stdout.write(JSON.stringify({ image: true, split: 2, chained: true, redelivered: 2, large_get: true }));
} finally { await ctx.fiber.dispose(); }
