#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, readFileSync, realpathSync } from "node:fs";
import { mkdir, readFile, writeFile, rename, symlink, readlink, rm, open } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const PACKAGE = "@mmp/deepseek-harness";
const BEGIN = "# mmp-local-deploy:begin";
const END = "# mmp-local-deploy:end";
const PROFILE_FILES = ["package.json", "pnpm-lock.yaml", "cordis.patch.yml"];
const repository = resolve(dirname(fileURLToPath(import.meta.url)), "../..");

const help = `MMP DeepSeek Harness 本机部署（macOS launchd）

  node tools/deploy/deepseek-harness.mjs init --mmp-config <现有 mmp.json>
       --transfer-host <A 可访问的本机内网 IP> --service-label <已有 launchd label>
       --web-url <本机 Harness URL> --log-file <launchd 日志>
  node tools/deploy/deepseek-harness.mjs deploy [--ref origin/main]
  node tools/deploy/deepseek-harness.mjs status
  node tools/deploy/deepseek-harness.mjs rollback

通用选项：--config <私有配置 JSON>（默认 $DSH_HOME/mmp/deploy.json）
init 选项：--profile web --transfer-port 18765 --dsh-home <目录>

deploy 从 Git 固定提交构建独立目录，维护本插件配置并重启指定服务。
失败自动恢复；其他插件、会话和媒体数据不删除。密钥不进入命令参数或输出。
`;

function run(command, args, options = {}) {
  try { return execFileSync(command, args, { encoding: "utf8", maxBuffer: 64 * 1024 * 1024, stdio: ["ignore", "pipe", "pipe"], ...options }); }
  catch (error) { throw new Error(`${command} ${args[0] ?? ""} 失败（exit=${error.status ?? error.code}）`); }
}

function jsonFile(path) {
  try { return JSON.parse(readFileSync(path, "utf8")); }
  catch (error) { if (error instanceof SyntaxError) throw new Error(`JSON 文件格式无效：${path}`); throw error; }
}

export function patchProfile(original, configPath) {
  const start = original.indexOf(BEGIN);
  const end = original.indexOf(END);
  if ((start < 0) !== (end < 0) || (start >= 0 && end < start)) throw new Error("profile 中的 MMP 配置块不完整，请先修复");
  const prefix = start < 0 ? original : original.slice(0, start) + original.slice(end + END.length).replace(/^\r?\n/, "");
  const expression = `JSON.parse(process.getBuiltinModule('node:fs').readFileSync(${JSON.stringify(configPath)}, 'utf8')).plugin`;
  const content = prefix.split(/\r?\n/).filter((line) => line.trim() && !line.trimStart().startsWith("#")).join("\n").trim();
  const base = content === "[]" ? prefix.replace(/^[ \t]*\[\][ \t]*(?:\r?\n|$)/m, "") : prefix;
  return `${base}${base && !base.endsWith("\n") ? "\n" : ""}${BEGIN}\n- id: mmp\n  config: !!js ${JSON.stringify(expression)}\n${END}\n`;
}

async function optionalFile(path) {
  try { return await readFile(path); } catch (error) { if (error.code === "ENOENT") return null; throw error; }
}

export async function snapshot(profile, current, sha) {
  const files = {};
  for (const name of PROFILE_FILES) {
    const bytes = await optionalFile(join(profile, name));
    files[name] = bytes === null ? null : bytes.toString("base64");
  }
  let previous = null;
  try { previous = await readlink(current); } catch (error) { if (error.code !== "ENOENT") throw error; }
  return { sha, previous, files };
}

async function switchLink(current, target) {
  const temporary = `${current}.${randomUUID()}`;
  await symlink(target, temporary);
  try { await rename(temporary, current); } finally { await rm(temporary, { force: true }); }
}

export async function restore(record, profile, current) {
  if (record.previous === null) await rm(current, { force: true });
  else await switchLink(current, record.previous);
  for (const name of PROFILE_FILES) {
    const path = join(profile, name);
    if (record.files[name] === null) await rm(path, { force: true });
    else await writeFile(path, Buffer.from(record.files[name], "base64"), { mode: 0o600 });
  }
}

async function privateJson(path, value) {
  await mkdir(dirname(path), { recursive: true, mode: 0o700 });
  const temporary = `${path}.${randomUUID()}`;
  await writeFile(temporary, JSON.stringify(value, null, 2) + "\n", { mode: 0o600, flag: "wx" });
  await rename(temporary, path);
}

export function loadConfig(path) {
  const config = jsonFile(path);
  for (const key of ["dshHome", "root", "logFile"]) {
    if (!config[key] || !config[key].startsWith("/")) throw new Error(`配置 ${key} 必须是绝对路径`);
  }
  if (!/^[a-zA-Z0-9._-]+$/.test(config.profile) || !/^[a-zA-Z0-9._-]+$/.test(config.serviceLabel)) throw new Error("无效的 profile 或 serviceLabel");
  for (const url of [config.webUrl, config.plugin?.baseUrl, config.plugin?.transferBaseUrl]) {
    const parsed = new URL(url);
    if (!/^https?:$/.test(parsed.protocol) || parsed.username || parsed.password || parsed.pathname !== "/" || parsed.search || parsed.hash) throw new Error("配置 URL 必须是无凭据的 HTTP(S) origin");
  }
  if (!config.plugin.transferHost || ["0.0.0.0", "::"].includes(config.plugin.transferHost)) throw new Error("必须指定本机具体内网地址");
  if (!Number.isInteger(config.plugin.transferPort) || config.plugin.transferPort < 1 || config.plugin.transferPort > 65535) throw new Error("无效的 transferPort");
  config.profilePath = join(config.dshHome, "profiles", config.profile);
  config.current = join(config.root, "current");
  config.statePath = join(config.root, "state.json");
  return config;
}

async function init(path, options) {
  if (existsSync(path)) throw new Error("配置已存在，请直接编辑该文件；init 不覆盖密钥或现有配置");
  for (const key of ["mmp-config", "transfer-host", "service-label", "web-url", "log-file"]) if (!options[key]) throw new Error(`init 缺少 --${key}`);
  const upstream = jsonFile(resolve(options["mmp-config"]));
  const dshHome = resolve(options["dsh-home"] ?? process.env.DSH_HOME ?? join(homedir(), ".dsh"));
  const port = Number(options["transfer-port"] ?? 18765);
  const host = options["transfer-host"];
  const config = {
    dshHome, root: join(dshHome, "mmp", "plugin", options.profile ?? "web"), profile: options.profile ?? "web",
    serviceLabel: options["service-label"], webUrl: new URL(options["web-url"]).origin,
    logFile: resolve(options["log-file"]),
    plugin: { baseUrl: upstream.baseUrl, ...(upstream.apiKey ? { apiKey: upstream.apiKey } : {}),
      transferHost: host, transferPort: port, transferBaseUrl: `http://${host.includes(":") ? `[${host}]` : host}:${port}` },
  };
  await privateJson(path, config);
  try { loadConfig(path); } catch (error) { await rm(path); throw error; }
  console.log(`已创建私有部署配置：${path}（权限 600；未输出密钥）`);
}

async function tail(path) {
  const file = await open(path);
  try {
    const { size } = await file.stat();
    const buffer = Buffer.alloc(Math.min(size, 128 * 1024));
    await file.read(buffer, 0, buffer.length, Math.max(0, size - buffer.length));
    return buffer.toString("utf8");
  } finally { await file.close(); }
}

export async function runtimeInventory(config) {
  // 仅在内存中交换本机启动令牌；不打印日志、令牌或 Cookie。
  const tokens = [...(await tail(config.logFile)).matchAll(/[?&]token=([a-zA-Z0-9._~-]+)/g)];
  if (!tokens.length) throw new Error("日志末尾未找到 Harness 启动 URL，无法验证认证后的插件状态");
  const exchange = await fetch(`${config.webUrl}/?token=${tokens.at(-1)[1]}`, { redirect: "manual", signal: AbortSignal.timeout(5000) })
    .catch(() => { throw new Error("无法连接 Harness Web"); });
  const cookie = exchange.headers.get("set-cookie")?.split(";", 1)[0];
  if (![302, 303].includes(exchange.status) || !cookie) throw new Error("Harness 启动令牌交换失败");
  const endpoint = "pluginManager/listPlugins";
  const response = await fetch(`${config.webUrl}/api/${endpoint}`, {
    method: "POST", headers: { "content-type": "application/json", cookie },
    body: JSON.stringify({ type: "client-request", rpcId: randomUUID(), method: endpoint, payload: { args: {} } }),
    signal: AbortSignal.timeout(5000),
  }).catch(() => { throw new Error("无法查询 Harness 插件清单"); });
  if (!response.ok) throw new Error(`Harness 插件状态接口 HTTP ${response.status}`);
  const body = await response.json();
  if (!body.result?.ok || !Array.isArray(body.result.value)) throw new Error("Harness 未返回有效插件清单");
  return body.result.value;
}

export async function verify(config, baseline = [], expectMmp = true) {
  const inventory = await runtimeInventory(config);
  const active = inventory.filter((entry) => entry.enabled && entry.fiberPhase === "active");
  const mmp = active.find((entry) => entry.moduleName === PACKAGE);
  if (!!mmp !== expectMmp) throw new Error(expectMmp ? "MMP 插件没有处于 active 状态" : "回滚后仍残留活动的 MMP 插件");
  const counts = (items) => items.reduce((map, name) => map.set(name, (map.get(name) ?? 0) + 1), new Map());
  const now = counts(active.map((entry) => entry.moduleName));
  for (const [name, count] of counts(baseline.filter((name) => name !== PACKAGE))) {
    if ((now.get(name) ?? 0) < count) throw new Error(`原有插件未恢复 active：${name}`);
  }
  if (expectMmp) {
    const health = await fetch(`${config.plugin.baseUrl}/health`, {
      headers: config.plugin.apiKey ? { authorization: `Bearer ${config.plugin.apiKey}` } : {}, signal: AbortSignal.timeout(5000),
    }).catch(() => { throw new Error("内网 MMP health 不可达"); });
    if (!health.ok || (await health.json()).status !== "ok") throw new Error("内网 MMP health 未就绪");
    const transfer = await fetch(`${config.plugin.transferBaseUrl}/mmp-transfer/deployment-check`, { signal: AbortSignal.timeout(5000) })
      .catch(() => { throw new Error("MMP 内网传输监听不可达"); });
    if (transfer.status !== 404) throw new Error("MMP 内网传输监听未就绪");
  }
  return { mmp: !!mmp, activePlugins: active.length, baselinePreserved: true };
}

function restart(config) {
  if (process.platform !== "darwin") throw new Error("本流程只管理 macOS launchd 服务");
  run("launchctl", ["kickstart", "-k", `gui/${process.getuid()}/${config.serviceLabel}`]);
}

async function waitReady(config, baseline, expectMmp = true) {
  let last;
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    try { return await verify(config, baseline, expectMmp); } catch (error) { last = error; }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw last;
}

async function buildRelease(config, ref) {
  if (ref === "origin/main") run("git", ["fetch", "origin"], { cwd: repository });
  const sha = run("git", ["rev-parse", "--verify", `${ref}^{commit}`], { cwd: repository }).trim();
  const releases = join(config.root, "releases");
  const target = join(releases, sha);
  const packagePath = join(target, "ts", "packages", "deepseek-harness");
  await mkdir(releases, { recursive: true, mode: 0o700 });
  if (!existsSync(target)) {
    const staging = join(releases, `.staging-${randomUUID()}`);
    const archive = `${staging}.tar`;
    try {
      await mkdir(staging, { mode: 0o700 });
      const bytes = run("git", ["archive", "--format=tar", sha, "ts", "protocol/schemas"], { cwd: repository, encoding: null });
      await writeFile(archive, bytes, { mode: 0o600 });
      run("tar", ["-xf", archive, "-C", staging]);
      const cwd = join(staging, "ts");
      const env = { ...process.env, NO_PROXY: "127.0.0.1,localhost,git.seanartech.com,mmp.seanartech.com", no_proxy: "127.0.0.1,localhost,git.seanartech.com,mmp.seanartech.com" };
      run("pnpm", ["install", "--frozen-lockfile"], { cwd, env, stdio: "inherit" });
      run("pnpm", ["--filter", `${PACKAGE}...`, "build"], { cwd, env, stdio: "inherit" });
      await rename(staging, target);
    } finally { await rm(staging, { recursive: true, force: true }); await rm(archive, { force: true }); }
  }
  const manifest = JSON.parse(await readFile(join(packagePath, "package.json"), "utf8"));
  if (run("dsh", ["--version"]).trim() !== manifest.engines.dsh) throw new Error(`插件需要 dsh ${manifest.engines.dsh}，不自动升级 Harness`);
  if (!existsSync(join(packagePath, "dist", "index.js")) || !existsSync(join(packagePath, "dist", "client.js"))) throw new Error("版本目录缺少构建产物");
  return { sha, packagePath };
}

async function deploy(config, ref) {
  const baseline = (await runtimeInventory(config)).filter((entry) => entry.enabled && entry.fiberPhase === "active").map((entry) => entry.moduleName);
  const { sha, packagePath } = await buildRelease(config, ref);
  const record = { ...await snapshot(config.profilePath, config.current, sha), baseline };
  const previousBytes = await optionalFile(config.statePath);
  const previousState = previousBytes ? JSON.parse(previousBytes) : null;
  const transaction = join(config.root, "history", `${randomUUID()}.json`);
  const runtimeConfig = join(config.root, "configs", `${randomUUID()}.json`);
  await privateJson(runtimeConfig, { plugin: config.plugin });
  await privateJson(transaction, { ...record, previousState });
  const patchPath = join(config.profilePath, "cordis.patch.yml");
  const patch = patchProfile((await optionalFile(patchPath))?.toString("utf8") ?? "[]\n", runtimeConfig);
  let changed = false;
  try {
    changed = true;
    await switchLink(config.current, packagePath);
    await writeFile(patchPath, patch, { mode: 0o600 });
    run("dsh", ["plugin", "--profile", config.profile, "add", config.current], { env: { ...process.env, DSH_HOME: config.dshHome }, stdio: "inherit" });
    run("dsh", ["--profile", config.profile, "--dump-config"], { env: { ...process.env, DSH_HOME: config.dshHome } });
    restart(config);
    const health = await waitReady(config, baseline);
    await privateJson(config.statePath, { sha, transaction, runtimeConfig });
    changed = false;
    console.log(JSON.stringify({ deployed: sha, ...health }));
  } catch (error) {
    if (changed) {
      try {
        await restore(record, config.profilePath, config.current);
        restart(config);
        await waitReady(await runningConfig(config, previousState), baseline, baseline.includes(PACKAGE));
        console.error("部署失败，已恢复部署前的版本与 profile 配置。");
      } catch (recovery) {
        throw new Error(`${error.message}；自动恢复失败：${recovery.message}。恢复快照：${transaction}`);
      }
    }
    throw error;
  }
}

async function rollback(config) {
  const state = JSON.parse(await readFile(config.statePath, "utf8"));
  const record = JSON.parse(await readFile(state.transaction, "utf8"));
  await restore(record, config.profilePath, config.current);
  restart(config);
  const health = await waitReady(await runningConfig(config, record.previousState), record.baseline, record.baseline.includes(PACKAGE));
  if (record.previousState === null) await rm(config.statePath);
  else await privateJson(config.statePath, record.previousState);
  console.log(JSON.stringify({ rolledBack: state.sha, ...health }));
}

async function runningConfig(config, state) {
  return state ? { ...config, plugin: jsonFile(state.runtimeConfig).plugin } : config;
}

export async function main(argv = process.argv.slice(2)) {
  const [command, ...rest] = argv;
  if (!command || command === "--help" || command === "help") { console.log(help); return; }
  const options = {};
  for (let index = 0; index < rest.length; index += 2) {
    if (!rest[index].startsWith("--") || rest[index + 1] === undefined) throw new Error("选项必须使用 --name value");
    options[rest[index].slice(2)] = rest[index + 1];
  }
  const path = resolve(options.config ?? join(process.env.DSH_HOME ?? join(homedir(), ".dsh"), "mmp", "deploy.json"));
  if (command === "init") { await init(path, options); return; }
  const config = loadConfig(path);
  if (command === "status") {
    const bytes = await optionalFile(config.statePath);
    if (bytes === null) {
      const inventory = await runtimeInventory(config);
      console.log(JSON.stringify({ managed: false, mmp: inventory.some((entry) => entry.moduleName === PACKAGE && entry.enabled && entry.fiberPhase === "active") }));
      return;
    }
    const state = JSON.parse(bytes);
    const record = JSON.parse(await readFile(state.transaction, "utf8"));
    console.log(JSON.stringify({ sha: state.sha, source: realpathSync(config.current), ...await verify(await runningConfig(config, state), record.baseline) }));
    return;
  }
  if (!["deploy", "rollback"].includes(command)) throw new Error(`未知命令 ${command}`);
  await mkdir(config.root, { recursive: true, mode: 0o700 });
  const lock = await open(join(config.root, "deploy.lock"), "wx", 0o600).catch((error) => { if (error.code === "EEXIST") throw new Error("部署锁已存在；确认没有其他部署进程后再清理 deploy.lock"); throw error; });
  try {
    await lock.writeFile(String(process.pid));
    if (command === "deploy") await deploy(config, options.ref ?? "origin/main");
    else await rollback(config);
  } finally { await lock.close(); await rm(join(config.root, "deploy.lock"), { force: true }); }
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  main().catch((error) => { console.error(`部署流程失败：${error.message.replace(/token=[^\s&]+/g, "token=<redacted>")}`); process.exitCode = 1; });
}
