/**
 * protocol/schemas/*.schema.json → src/types.ts。$ref 用的是 urn:mmp:protocol:1:<name>，这里给 ref-parser 一个 urn 解析器。
 * 改了 schema 就重跑 `pnpm gen-types` 并提交生成物；CI 会比对生成物是否过期。
 */
import { compile } from "json-schema-to-typescript";
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const SCHEMAS = resolve(here, "../../../../protocol/schemas");
const OUT = resolve(here, "../src/types.ts");

const files = readdirSync(SCHEMAS).filter((f) => f.endsWith(".schema.json")).sort();
const byId = new Map<string, any>();
for (const f of files) {
  const s = JSON.parse(readFileSync(join(SCHEMAS, f), "utf8"));
  byId.set(s.$id, s);
}

const urnResolver = {
  order: 1,
  canRead: /^urn:mmp:protocol:/,
  read: (file: { url: string }) => {
    const id = file.url.split("#")[0];
    const s = byId.get(id);
    if (!s) throw new Error(`unknown schema ${id}`);
    return JSON.stringify(s);
  },
};

// 合成一个根，把六个 schema 的根与 job-api 的 $defs 都挂上去，一次 compile：每个类型只输出一份
const root: any = { $id: "urn:mmp:protocol:1:all", title: "Protocol", type: "object", additionalProperties: false, properties: {} };
for (const [id, s] of byId) {
  const name = id.split(":").pop()!;
  if (s.type || s.oneOf || s.anyOf) root.properties[name] = { $ref: id };
  for (const def of Object.keys(s.$defs ?? {})) root.properties[`${name}__${def}`] = { $ref: `${id}#/$defs/${def}` };
}
const ts = await compile(root, "Protocol", {
  bannerComment: "/* 由 scripts/gen-types.ts 从 protocol/schemas 生成，不要手改：pnpm gen-types */\n/* eslint-disable */",
  cwd: SCHEMAS,
  declareExternallyReferenced: true,
  additionalProperties: true,
  unknownAny: true,
  $refOptions: { resolve: { urn: urnResolver as any, file: false, http: false } as any },
});
writeFileSync(OUT, ts);
console.log(`wrote ${OUT} (${ts.length} chars)`);
