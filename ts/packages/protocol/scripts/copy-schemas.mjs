// build 后把仓库根 protocol/schemas/*.schema.json 复制进包内 schemas/，发布的包自带 schema。
import { copyFileSync, mkdirSync, readdirSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
const here = dirname(fileURLToPath(import.meta.url));
const src = resolve(here, "../../../../protocol/schemas");
const dst = resolve(here, "../schemas");
rmSync(dst, { recursive: true, force: true });
mkdirSync(dst, { recursive: true });
let n = 0;
for (const f of readdirSync(src).filter((f) => f.endsWith(".schema.json"))) { copyFileSync(join(src, f), join(dst, f)); n++; }
console.log(`copied ${n} schemas → ${dst}`);
