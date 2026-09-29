/**
 * 契约测试（ts 侧）。目录约定与 ../py/conftest.py 一致：
 *   protocol/fixtures/<schema>/<def>/{valid,invalid}/<name>.json
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import Ajv2020, { type ValidateFunction } from "ajv/dist/2020.js";
import addFormats from "ajv-formats";

const PROTOCOL = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const SCHEMAS = join(PROTOCOL, "schemas");
const FIXTURES = join(PROTOCOL, "fixtures");

type Schema = { $id: string; $schema: string; $defs?: Record<string, unknown> };

const schemas = new Map<string, Schema>();
for (const f of readdirSync(SCHEMAS).filter((n) => n.endsWith(".schema.json")).sort()) {
  schemas.set(f.replace(/\.schema\.json$/, ""), JSON.parse(readFileSync(join(SCHEMAS, f), "utf8")));
}

// strictSchema（未知关键字报错）保持默认开启；strictRequired 是 ajv 对 oneOf 分支写法的风格意见，不开。
const ajv = new Ajv2020({ strictSchema: true, strictTypes: "log", strictTuples: true, strictRequired: false, allErrors: true, validateFormats: true });
addFormats(ajv, ["date-time", "uri"]);
for (const s of schemas.values()) ajv.addSchema(s);

function validatorFor(schemaName: string, defn: string): ValidateFunction {
  const root = schemas.get(schemaName)!;
  if (defn === "root") return ajv.getSchema(root.$id)!;
  assert.ok(root.$defs && defn in root.$defs, `${schemaName} has no $defs/${defn}`);
  const key = `${root.$id}#/$defs/${defn}`;
  return ajv.getSchema(key) ?? ajv.compile({ $schema: root.$schema, $ref: key });
}

type Case = { schema: string; defn: string; valid: boolean; file: string };
function* iterFixtures(): Generator<Case> {
  for (const schema of readdirSync(FIXTURES).sort()) {
    if (!statSync(join(FIXTURES, schema)).isDirectory()) continue;
    for (const defn of readdirSync(join(FIXTURES, schema)).sort()) {
      for (const verdict of ["valid", "invalid"] as const) {
        const dir = join(FIXTURES, schema, defn, verdict);
        let files: string[] = [];
        try { files = readdirSync(dir).filter((n) => n.endsWith(".json")).sort(); } catch { continue; }
        for (const file of files) yield { schema, defn, valid: verdict === "valid", file: join(dir, file) };
      }
    }
  }
}

test("schemas compile under ajv strict mode and carry versioned $id", () => {
  for (const [name, s] of schemas) {
    assert.equal(s.$schema, "https://json-schema.org/draft/2020-12/schema", name);
    assert.equal(s.$id, `urn:mmp:protocol:2:${name}`, name);
    assert.ok(ajv.getSchema(s.$id), `${name} did not compile`);
  }
});

test("format validation is active", () => {
  const v = ajv.compile({ type: "string", format: "date-time" });
  assert.equal(v("yesterday"), false);
  assert.equal(v("2026-09-10T17:00:00+08:00"), true);
});

test("every schema has at least one valid fixture", () => {
  for (const name of schemas.keys()) {
    const found = [...iterFixtures()].some((c) => c.schema === name && c.valid);
    assert.ok(found, `no valid fixtures for ${name}`);
  }
});

let count = 0;
for (const c of iterFixtures()) {
  count++;
  const id = `${c.schema}/${c.defn}/${c.valid ? "valid" : "invalid"}/${c.file.split("/").pop()}`;
  test(id, () => {
    const validate = validatorFor(c.schema, c.defn);
    const ok = validate(JSON.parse(readFileSync(c.file, "utf8")));
    if (c.valid) assert.ok(ok, ajv.errorsText(validate.errors, { separator: "\n" }));
    else assert.equal(ok, false, "expected INVALID but passed");
  });
}

test("fixture count matches the python side's expectation", () => {
  assert.ok(count >= 100, `only ${count} fixtures found`);
});

test("M0 digest sample points at the real test wav hash", () => {
  const d = JSON.parse(readFileSync(join(FIXTURES, "digest/root/valid/m0_hum_then_speech.json"), "utf8"));
  assert.equal(d.media_id, "sha256:6b4a2b8c89e0bcf240920e7922d8d77666f7b61a11ba2ba7389f8156986220f1");
  assert.equal(d.segments[0].label_status, "unclassified");
  assert.deepEqual(d.segments[0].labels, []);
  assert.ok(!("caption" in (d.global ?? {})));
});
