import { homedir } from "node:os";
import { resolve } from "node:path";

export interface Config {
  /** A 所绑定的 B-py 任务 API；不启用远端 broker。 */
  baseUrl: string;
  apiKey?: string;
  /** 内网传输监听地址与 A 能访问的回调 URL（含端口）。 */
  transferHost: string;
  transferPort?: number;
  transferBaseUrl: string;
  dataDir?: string;
  timeoutMs?: number;
  preflightTimeoutMs?: number;
  maxTransferBytes?: number;
}

export function configure(input: Config) {
  const config = {
    ...input,
    apiKey: input.apiKey ?? process.env.MMP_API_KEY,
    transferPort: input.transferPort ?? 18765,
    dataDir: resolve(input.dataDir ?? resolve(process.env.DSH_HOME ?? resolve(homedir(), ".dsh"), "mmp")),
    timeoutMs: input.timeoutMs ?? 600_000,
    preflightTimeoutMs: input.preflightTimeoutMs ?? 60_000,
    maxTransferBytes: input.maxTransferBytes ?? 1024 * 1024 * 1024,
  };
  for (const key of ["baseUrl", "transferBaseUrl"] as const) {
    const url = new URL(config[key]);
    if (!/^https?:$/.test(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== "/") {
      throw new Error(`${key} must be an HTTP(S) origin without credentials`);
    }
    config[key] = url.origin;
  }
  if (!config.transferHost || ["0.0.0.0", "::"].includes(config.transferHost)) throw new Error("transferHost must name a concrete local interface");
  if (!Number.isInteger(config.transferPort) || config.transferPort < 0 || config.transferPort > 65535) throw new Error("invalid transferPort");
  for (const key of ["timeoutMs", "preflightTimeoutMs", "maxTransferBytes"] as const) {
    if (!Number.isSafeInteger(config[key]) || config[key] <= 0) throw new Error(`invalid ${key}`);
  }
  return config;
}
export type Settings = ReturnType<typeof configure>;
