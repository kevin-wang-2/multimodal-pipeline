import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import * as React from "react";

test("published client artifact loads through the real Harness module-factory contract", async () => {
  const code = await readFile(new URL("../dist/client.js", import.meta.url), "utf8");
  let loaded: { id: string; factory: (require: (name: string) => unknown) => any } | undefined;
  runInNewContext(code, { window: { __ModuleLoader__: { load(value: typeof loaded) { loaded = value; } } } });
  assert.equal(loaded!.id, "@mmp/deepseek-harness");
  const plugin = loaded!.factory((name) => { assert.equal(name, "react"); return React; });
  assert.equal(typeof plugin.apply, "function");
  assert.deepEqual([...plugin.inject], ["slots"]);
  const entries: unknown[] = [];
  plugin.apply({ slots: { inject(name: string, mount: () => unknown) { assert.equal(name, "tool.call.toolview"); mount(); }, register(entry: unknown) { entries.push(entry); } } });
  assert.equal((entries[0] as { key: string }).key, "mmp_run");
});
