import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtemp, mkdir, readFile, writeFile, readlink, symlink, rm, stat } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { runInNewContext } from "node:vm";
import { patchProfile, snapshot, restore, verify, main } from "./deepseek-harness.mjs";

test("managed profile patch preserves unrelated YAML and evaluates only the private plugin config", () => {
  const original = "- id: other\n  config:\n    value: !!js process.env.SOMETHING\n";
  const path = "/private/config with # and : spaces.json";
  const patched = patchProfile(original, path);
  assert.ok(patched.startsWith(original));
  assert.equal(patchProfile(patched, path), patched);
  assert.ok(!patchProfile("[]\n", path).startsWith("[]"));
  const scalar = patched.split("  config: !!js ").at(-1).split("\n")[0];
  const expression = JSON.parse(scalar);
  const plugin = { baseUrl: "http://node.test", apiKey: "private" };
  const result = runInNewContext(expression, { process: { getBuiltinModule(name) {
    assert.equal(name, "node:fs");
    return { readFileSync(actual) { assert.equal(actual, path); return JSON.stringify({ plugin, serviceLabel: "private-service" }); } };
  } } });
  assert.deepEqual(JSON.parse(JSON.stringify(result)), plugin);
  assert.doesNotMatch(patched, /apiKey|private-service/);
  assert.throws(() => patchProfile("# mmp-local-deploy:begin\n", path), /不完整/);
});

test("rollback restores exact profile files and previous source; first install removes only its own additions", async () => {
  const root = await mkdtemp(join(tmpdir(), "mmp-deploy-rollback-"));
  try {
    const profile = join(root, "profile");
    const current = join(root, "current");
    await mkdir(profile);
    const before = { "package.json": '{"dependencies":{"other":"1"}}\n', "cordis.patch.yml": "- id: other\n" };
    for (const [name, bytes] of Object.entries(before)) await writeFile(join(profile, name), bytes);
    const first = await snapshot(profile, current, "first");
    await symlink(root, current);
    await writeFile(join(profile, "package.json"), "changed");
    await writeFile(join(profile, "pnpm-lock.yaml"), "new lock");
    await restore(first, profile, current);
    await assert.rejects(readlink(current), { code: "ENOENT" });
    await assert.rejects(stat(join(profile, "pnpm-lock.yaml")), { code: "ENOENT" });
    for (const [name, bytes] of Object.entries(before)) assert.equal(await readFile(join(profile, name), "utf8"), bytes);
    await symlink(root, current);
    const upgrade = await snapshot(profile, current, "next");
    await rm(current);
    await symlink(profile, current);
    await restore(upgrade, profile, current);
    assert.equal(await readlink(current), root);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("authenticated deployment check verifies active plugin, preserves other plugins, and checks upstream and transfer", async () => {
  const root = await mkdtemp(join(tmpdir(), "mmp-deploy-check-"));
  let rows = [
    { moduleName: "other", enabled: true, fiberPhase: "active" },
    { moduleName: "@mmp/deepseek-harness", enabled: true, fiberPhase: "active" },
  ];
  let upstreamHealthy = true;
  const server = createServer(async (req, res) => {
    if (req.url.startsWith("/?token=")) { res.writeHead(303, { "set-cookie": "owner=private; HttpOnly", location: "./" }).end(); return; }
    if (req.url === "/api/pluginManager/listPlugins") {
      assert.equal(req.headers.cookie, "owner=private");
      let data = ""; for await (const chunk of req) data += chunk;
      const request = JSON.parse(data);
      assert.deepEqual(request.payload, { args: {} });
      res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({ type: "server-response", rpcId: request.rpcId, result: { ok: true, value: rows } })); return;
    }
    if (req.url === "/health") {
      assert.equal(req.headers.authorization, "Bearer upstream-private");
      res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({ status: upstreamHealthy ? "ok" : "unavailable" })); return;
    }
    res.writeHead(404).end();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const origin = `http://127.0.0.1:${server.address().port}`;
    const logFile = join(root, "web.log");
    await writeFile(logFile, `dsh web: ${origin}/?token=launch-private\n`);
    const config = { webUrl: origin, logFile, plugin: { baseUrl: origin, apiKey: "upstream-private", transferBaseUrl: origin } };
    assert.deepEqual(await verify(config, ["other"]), { mmp: true, activePlugins: 2, baselinePreserved: true });
    rows = rows.filter((row) => row.moduleName !== "other");
    await assert.rejects(verify(config, ["other"]), /原有插件/);
    upstreamHealthy = false;
    await assert.rejects(verify(config), /health/);
    rows = [];
    assert.equal((await verify(config, [], false)).mmp, false);
  } finally { server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); await rm(root, { recursive: true, force: true }); }
});

test("init imports credentials into a private local config and never overwrites an existing one", async () => {
  const root = await mkdtemp(join(tmpdir(), "mmp-deploy-init-"));
  const originalLog = console.log;
  let output = "";
  console.log = (text) => { output += text; };
  try {
    const upstream = join(root, "upstream.json");
    const config = join(root, "deploy.json");
    await writeFile(upstream, JSON.stringify({ baseUrl: "http://node.test", apiKey: "do-not-print" }));
    const args = ["init", "--config", config, "--mmp-config", upstream, "--transfer-host", "127.0.0.1", "--service-label", "local.test", "--web-url", "http://127.0.0.1:3080", "--log-file", join(root, "log"), "--dsh-home", root];
    await main(args);
    assert.equal(JSON.parse(await readFile(config, "utf8")).plugin.apiKey, "do-not-print");
    if (process.platform !== "win32") assert.equal((await stat(config)).mode & 0o777, 0o600);
    assert.doesNotMatch(output, /do-not-print/);
    await assert.rejects(main(args), /不覆盖/);
  } finally { console.log = originalLog; await rm(root, { recursive: true, force: true }); }
});
