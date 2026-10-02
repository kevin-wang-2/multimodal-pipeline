import type { CapabilitiesResponse, Capability } from "@mmp/protocol";
import { MmpError } from "./errors.js";

export interface CapabilityQuery {
  purpose: string;
  contentType: string;
}

function bareContentType(value: string): string {
  return value.split(";", 1)[0].trim().toLowerCase();
}

export function matchesMediaType(pattern: string, contentType: string): boolean {
  const candidate = bareContentType(contentType);
  const accepted = pattern.toLowerCase();
  if (accepted === "*/*") return candidate.includes("/");
  if (accepted.endsWith("/*")) return candidate.startsWith(accepted.slice(0, -1));
  return candidate === accepted;
}

function specificity(pattern: string): number {
  return pattern === "*/*" ? 0 : pattern.endsWith("/*") ? 1 : 2;
}

/** purpose + Content-Type 解析唯一能力；未知或歧义都显式失败。 */
export function resolveCapability(response: CapabilitiesResponse, query: CapabilityQuery): Capability {
  const matches = response.capabilities.flatMap(({ capability }) => {
    if (capability.purpose !== query.purpose) return [];
    const accepts = capability.input.media.accepts.filter((pattern) => matchesMediaType(pattern, query.contentType));
    return accepts.length ? [{ capability, rank: Math.max(...accepts.map(specificity)) }] : [];
  });
  if (!matches.length) {
    throw new MmpError("unsupported_type", `no capability for purpose=${query.purpose} content_type=${bareContentType(query.contentType)}`);
  }
  const bestRank = Math.max(...matches.map(({ rank }) => rank));
  const best = matches.filter(({ rank }) => rank === bestRank);
  if (best.length !== 1) {
    throw new MmpError("unexpected", `ambiguous capabilities for purpose=${query.purpose} content_type=${bareContentType(query.contentType)}: ${best.map(({ capability }) => capability.id).join(", ")}`);
  }
  return best[0].capability;
}

/** 按任务类型 id 按需取得一项完整能力描述；不会把全量 params schema 注入宿主提示。 */
export function describeCapability(response: CapabilitiesResponse, id: string): Capability {
  const matches = response.capabilities.filter(({ capability }) => capability.id === id);
  if (!matches.length) {
    throw new MmpError("unsupported_type", `unknown capability ${id}`);
  }
  if (matches.length !== 1) {
    throw new MmpError("unexpected", `ambiguous capability ${id}`);
  }
  return matches[0].capability;
}
