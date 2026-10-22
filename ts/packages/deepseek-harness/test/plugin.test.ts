import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Context } from "@deepseek-ai/cordis";
import SystemPrompt from "@deepseek-ai/dsh-system-prompt";
import Tools from "@deepseek-ai/dsh-tools";
import { ToolCallId } from "@deepseek-ai/dsh-llm";
import type { AttachmentStore } from "@deepseek-ai/dsh-attachment";
import * as plugin from "../src/index.js";
import { MediaResult } from "../src/client.js";
import { MemoryAttachments, capability, capabilities, done, mediaOutput, mediaOutField, signal, textOf } from "./helpers.js";

test("native Cordis plugin mounts, actual ToolRuntime executes generation, and authenticated route is registered and removed", async () => {
  const bytes = Buffer.from("output bytes");
  const cap = capability("image.generate", [{ name: "prompt", kind: "text", required: true, schema: { type: "string" } }], [mediaOutField("image")]);
  const server = createServer((req, res) => {
    res.setHeader("content-type", "application/json");
    if (req.url === "/capabilities") { res.end(JSON.stringify(capabilities(cap))); return; }
    res.end(JSON.stringify(done(cap.id, { image: mediaOutput(bytes, "image/png", true) })));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const directory = await mkdtemp(join(tmpdir(), "mmp-native-plugin-"));
  const ctx = new Context();
  let route: { path: string; fetch: (request: Request) => Promise<Response> } | undefined;
  let withdrawn = false;
  try {
    await ctx.plugin(SystemPrompt);
    await ctx.plugin(Tools);
    ctx.provide("attachments", new MemoryAttachments() as unknown as AttachmentStore);
    ctx.provide("agents", {} as Context["agents"]);
    ctx.provide("fs", {} as Context["fs"]);
    ctx.provide("connection", { fetch: { register(value: typeof route) { route = value; return async () => { withdrawn = true; }; } } } as unknown as Context["connection"]);
    const fiber = await ctx.plugin(plugin, {
      baseUrl: `http://127.0.0.1:${(server.address() as { port: number }).port}`,
      transferHost: "127.0.0.1", transferPort: 0, transferBaseUrl: "http://127.0.0.1", dataDir: directory,
    });
    assert.deepEqual(ctx.tools.schemas().map((schema) => schema.name), ["mmp_list", "mmp_describe", "mmp_run"]);
    const agent = { id: "sdk-test", ctx, session: { id: "sdk-test", header: { cwd: directory }, deriveMessages: () => [] } } as any;
    const execute = (name: string, args: unknown) => ctx.tools.execute({ name, arguments: args, callId: ToolCallId(`call-${name}`), signal: signal(), agent });
    const list = await execute("mmp_list", {});
    assert.equal(list.isError, false, textOf([list]));
    const described = await execute("mmp_describe", { id: cap.id });
    assert.equal(described.isError, false, textOf([described]));
    const result = await execute("mmp_run", { type: cap.id, arguments: { prompt: "test" } });
    assert.equal(result.isError, false, textOf([result]));
    assert.ok(result.content.some((block) => block.type === "file"));
    assert.equal(route!.path, "/api/mmp-media");
    const value = result.meta as any;
    const downloaded = await route!.fetch(new Request(`http://host${value.assets[0].href}`));
    assert.deepEqual(Buffer.from(await downloaded.arrayBuffer()), bytes);
    const rendered = MediaResult({ phase: "result", block: { ...result, kind: "tool-result", meta: value } } as any);
    const htmlShape = JSON.stringify(rendered);
    assert.match(htmlShape, /"type":"img"/);
    assert.match(htmlShape, /download=1/);
    assert.doesNotMatch(htmlShape, /mmp-transfer/);
    const removed = MediaResult({ phase: "result", block: { ...result, content: [{ type: "text", text: "withheld" }], meta: value } } as any);
    assert.doesNotMatch(JSON.stringify(removed), /"type":"img"/);
    for (const [contentType, element] of [["audio/wav", "audio"], ["video/mp4", "video"], ["application/pdf", "a"]]) {
      const meta = { ...value, assets: value.assets.map((asset: any) => ({ ...asset, contentType })) };
      assert.match(JSON.stringify(MediaResult({ phase: "result", block: { ...result, meta } } as any)), new RegExp(`"type":"${element}"`));
    }
    await fiber.dispose();
    assert.equal(withdrawn, true);
    assert.equal(ctx.tools.schemas().length, 0);
  } finally {
    await ctx.fiber.dispose();
    await new Promise<void>((resolve) => server.close(() => resolve()));
    await rm(directory, { recursive: true, force: true });
  }
});
