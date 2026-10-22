import test from "node:test";
import assert from "node:assert/strict";
import { createUserMessage } from "@deepseek-ai/dsh-llm";
import { Artifacts } from "../src/artifacts.js";
import { resultContent, Runtime } from "../src/runtime.js";
import { fixture, fakeFetch, capability, capabilities, mediaField, mediaOutField, mediaOutput, done, jobId, signal, textOf } from "./helpers.js";

test("preflight: audio + image, passthrough, PDF refusal, multi-turn and resume do not resubmit", async () => {
  const submits: any[] = [];
  const audio = capability("triage.audio", [mediaField("audio", 1, ["audio/*"])], [{ name: "digest", kind: "json", required: true, schema: {} }], "triage");
  const image = capability("triage.image", [mediaField("image", 1, ["image/*"])], [{ name: "digest", kind: "json", required: true, schema: {} }], "triage");
  const f = await fixture(fakeFetch((url, body) => {
    if (url.pathname === "/capabilities") return capabilities(audio, image);
    submits.push(body);
    return done(body.type, {}, { agent_context: { format: "mmp-agent-context-v1", content_type: "text/plain; charset=utf-8", text: `[mmp:agent-context start]\n${body.type} exact context\n[mmp:agent-context end]` } });
  }));
  try {
    const audioRef = await f.attachments.file(Buffer.from("RIFF\x04\0\0\0WAVEfmt "), "audio.wav");
    const imageFile = await f.attachments.file(Buffer.from("png"), "normalized.png");
    const imageRef = { ...imageFile, mediaType: "image/png" as const, width: 1, height: 1 };
    const pdf = await f.attachments.file(Buffer.from("%PDF-1.7"), "document.pdf");
    const message = createUserMessage({ source: { kind: "user" }, content: [{ type: "file", attachment: audioRef }, { type: "image", attachment: imageRef }, { type: "file", attachment: pdf }] });
    const result = await f.runtime.preflight([message], signal());
    assert.equal(result[0], message, "preserve user identity and native attachments");
    assert.deepEqual(submits.map((job) => job.type), ["triage.audio", "triage.image"]);
    assert.match(textOf(result), /triage.image exact context/);
    assert.match(textOf(result), /node_id=local/);
    assert.match(textOf(result), /unavailable.*\nattachment_id=.*reason=unsupported_type/);
    f.setMessages(result);
    const followup = createUserMessage({ source: { kind: "user" }, content: [{ type: "text", text: "继续" }] });
    for (let round = 0; round < 5; round++) assert.deepEqual(await f.runtime.preflight([followup], signal()), [followup]);
    const resumed = new Runtime(f.runtime.ctx, f.runtime.config);
    assert.deepEqual(await resumed.preflight([followup], signal()), [followup]);
    assert.equal(submits.length, 2);
  } finally { await f.close(); }
});

test("multi-output delivery, byte integrity, scalar/array fields, persistent artifacts and output-as-input", async () => {
  const first = Buffer.from("channel-one");
  const second = Buffer.from("channel-two");
  const cap = capability("audio.split", [mediaField("audio")], [mediaOutField("audio", 2), { name: "metadata", kind: "json", required: true, schema: {} }]);
  const concat = capability("audio.concat", [mediaField("audio", 32)], [mediaOutField("audio")]);
  const deliver = capability("media.deliver", [mediaField("media", 32)], [mediaOutField("media", 32)]);
  const calls: any[] = [];
  const f = await fixture(fakeFetch(async (url, body) => {
    if (url.pathname === "/capabilities") return capabilities(cap, concat, deliver);
    calls.push(body);
    if (body.type === "media.deliver") {
      const data = body.arguments.media[0].ref === mediaOutput(first).media_id ? first : second;
      const response = await fetch(body.delivery.media[0].url, { method: "PUT", body: data });
      assert.equal(response.status, 204);
      return done(body.type, { media: [{ ...mediaOutput(data), delivered: true }] });
    }
    if (body.type === "audio.concat") {
      assert.deepEqual(body.arguments.audio.map((h: any) => Buffer.from(h.inline, "base64").toString()), [first.toString(), second.toString()]);
      return done(body.type, { audio: mediaOutput(Buffer.concat([first, second]), "audio/wav", true) });
    }
    return done(body.type, { audio: [mediaOutput(first), mediaOutput(second)], metadata: { kind: "json", value: { channels: 2 } } });
  }));
  try {
    const ref = await f.attachments.file(Buffer.from("input"), "in.wav");
    const input = createUserMessage({ source: { kind: "user" }, content: [{ type: "file", attachment: ref }] });
    f.setMessages([input]);
    const result = await f.runtime.run({ type: "audio.split", arguments: { audio: { attachment_id: ref.attachmentId } } }, f.agent, signal());
    assert.equal(result.assets.length, 2);
    assert.ok(calls.filter((job) => job.type === "media.deliver").every((job) => job.node_id === "local"));
    assert.doesNotMatch(JSON.stringify(resultContent(result)), /inline|mmp-transfer/);
    const persisted = new Artifacts(f.runtime.artifacts["directory"], f.runtime.ctx.attachments);
    assert.deepEqual(await persisted.get(result.assets[0]!.id), result.assets[0]);
    const reply = await f.runtime.artifacts.serve(new Request(`http://host${result.assets[0]!.href}`, { headers: { range: "bytes=2-5" } }));
    assert.equal(reply.status, 206);
    assert.equal(await reply.text(), first.subarray(2, 6).toString());
    const joined = await f.runtime.run({ type: "audio.concat", arguments: { audio: result.assets.map((asset) => ({ asset_id: asset.id })) } }, f.agent, signal());
    assert.equal(joined.assets.length, 1);
    const downloaded = await f.runtime.artifacts.serve(new Request(`http://host${joined.assets[0]!.href}&download=1`));
    assert.equal(await downloaded.text(), Buffer.concat([first, second]).toString());
    assert.match(downloaded.headers.get("content-disposition")!, /^attachment/);
    await assert.rejects(f.runtime.run({ type: "audio.split", arguments: { audio: { ref: mediaOutput(first).media_id } } }, f.agent, signal()), /requires node_id/);
  } finally { await f.close(); }
});

test("large input is streamed through an ephemeral GET and ticket is retired after job", async () => {
  const data = Buffer.alloc(8 * 1024 * 1024 + 1, 42);
  const cap = capability("audio.inspect", [mediaField("audio")], [{ name: "metadata", kind: "json", required: true, schema: {} }]);
  let address = "";
  const f = await fixture(fakeFetch(async (url, body) => {
    if (url.pathname === "/capabilities") return capabilities(cap);
    address = body.arguments.audio.get.url;
    assert.equal(body.arguments.audio.inline, undefined);
    const downloaded = Buffer.from(await (await fetch(address)).arrayBuffer());
    assert.deepEqual(downloaded, data);
    return done(body.type, { metadata: { kind: "json", value: { bytes: data.length } } });
  }));
  try {
    const ref = await f.attachments.file(data, "large.wav");
    f.setMessages([createUserMessage({ source: { kind: "user" }, content: [{ type: "file", attachment: ref }] })]);
    await f.runtime.run({ type: cap.id, arguments: { audio: { attachment_id: ref.attachmentId } } }, f.agent, signal());
    assert.equal((await fetch(address)).status, 404);
  } finally { await f.close(); }
});

test("timeout cancels A job and preflight reports degradation; explicit Stop rejects", async () => {
  const cap = capability("triage.audio", [mediaField("audio", 1, ["audio/*"])], [{ name: "digest", kind: "json", required: true, schema: {} }], "triage");
  let cancellations = 0;
  const f = await fixture(fakeFetch(async (url, body, init) => {
    if (url.pathname === "/capabilities") return capabilities(cap);
    if (init.method === "DELETE") { cancellations++; return { job_id: jobId, status: "cancelled", type: cap.id, media_ids: {} }; }
    if (init.method === "POST") return { job_id: jobId, status: "queued", type: cap.id, media_ids: {} };
    return new Promise((_resolve, reject) => init.signal!.addEventListener("abort", () => reject(init.signal!.reason), { once: true }));
  }), { preflightTimeoutMs: 30 });
  try {
    const ref = await f.attachments.file(Buffer.from("input"), "audio.wav");
    const messages = [createUserMessage({ source: { kind: "user" }, content: [{ type: "file", attachment: ref }] })];
    const result = await f.runtime.preflight(messages, signal());
    assert.match(textOf(result), /reason=timeout/);
    assert.equal(cancellations, 1);
    const stop = new AbortController(); stop.abort(new Error("user stopped"));
    await assert.rejects(f.runtime.preflight(messages, stop.signal), /user stopped/);
  } finally { await f.close(); }
});

test("delivery corruption reports completed job refs without exposing transfer credentials", async () => {
  const data = Buffer.from("correct");
  const cap = capability("image.generate", [{ name: "prompt", kind: "text", required: true, schema: { type: "string" } }], [mediaOutField("image")]);
  const deliver = capability("media.deliver", [mediaField("media", 32)], [mediaOutField("media", 32)]);
  const f = await fixture(fakeFetch(async (url, body) => {
    if (url.pathname === "/capabilities") return capabilities(cap, deliver);
    if (body.type === "media.deliver") {
      await fetch(body.delivery.media[0].url, { method: "PUT", body: Buffer.from("corrupt") });
      return done(body.type, { media: [{ ...mediaOutput(data), delivered: true }] });
    }
    return done(body.type, { image: mediaOutput(data, "image/png") });
  }));
  try {
    await assert.rejects(f.runtime.run({ type: cap.id, arguments: { prompt: "example" } }, f.agent, signal()), (error: Error) => {
      assert.match(error.message, /Do not regenerate/);
      assert.match(error.message, /node_id=local/);
      assert.doesNotMatch(error.message, /mmp-transfer|http/);
      return true;
    });
  } finally { await f.close(); }
});
