import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

/** B 地址与 api_key：环境变量优先，其次 ~/.openclaw/mmp.json（chmod 600）。 */
export interface MmpHostConfig {
  baseUrl: string;
  apiKey?: string;
  /** 单个附件的等待上限（秒），默认 50：要小于 tools.media 条目的 timeoutSeconds */
  waitSec?: number;
  /** inline 上限（协议 8 MiB） */
  maxInlineBytes?: number;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env, file = join(homedir(), ".openclaw", "mmp.json")): MmpHostConfig {
  let fromFile: Partial<MmpHostConfig> = {};
  try { fromFile = JSON.parse(readFileSync(file, "utf8")); } catch { /* 没有配置文件也行 */ }
  const baseUrl = env.MMP_BASE_URL ?? fromFile.baseUrl;
  if (!baseUrl) throw new Error(`mmp: no baseUrl (set MMP_BASE_URL or ${file})`);
  return {
    baseUrl,
    apiKey: env.MMP_API_KEY ?? fromFile.apiKey,
    waitSec: fromFile.waitSec ?? 50,
    maxInlineBytes: fromFile.maxInlineBytes ?? 8 * 1024 * 1024,
  };
}
