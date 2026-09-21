/**
 * digest → 注入块：确定性模板（铁律 8），带边界标记，含 source 与降级标注。
 * 给纯文本主模型看的；目标是"够选工具、指对时间段"，不是复述一切。
 */
import type { Digest } from "@mmp/protocol";

export interface RenderOptions {
  /** 每段最多列几个标签 */
  maxLabels?: number;
  /** 段数超过此值时折叠中间段（长音频） */
  maxSegments?: number;
  /** 边界标记名 */
  tag?: string;
}

const fmt = (t: number) => t.toFixed(2);

export function renderDigest(d: Digest, o: RenderOptions = {}): string {
  const tag = o.tag ?? "mmp:digest";
  const maxLabels = o.maxLabels ?? 2;
  const maxSeg = o.maxSegments ?? 40;
  const src = d.source;
  const head = [`[${tag} start]`,
    `media=${d.media_id} kind=${d.kind} duration=${fmt(d.duration_sec)}s engine=${src.engine} tier=${src.tier} degraded=${src.degraded ? (src.degraded_reason ?? "yes") : "no"}`];
  if (src.degraded) head.push(`⚠ 本次预检降级（${src.degraded_reason ?? "unknown"}）：结果不完整，按需重新分析。`);

  const lines: string[] = [];
  lines.push(`时间轴（秒，[start,end)）：`);
  const segs = d.segments;
  // 协议 §7：asr 对象 = 语音段；asr === null = 非语音段（不适用）；asr 省略 = 语音段但 ASR 失败
  const render = (s: Digest["segments"][number]) => {
    const span = `[${fmt(s.start)}, ${fmt(s.end)})`;
    if (s.asr && typeof s.asr === "object") {
      const lang = s.asr.lang && s.asr.lang !== "n/a" ? ` (${s.asr.lang})` : "";
      return `- ${span} 语音：「${s.asr.text}」${lang}`;
    }
    if (s.asr === undefined) return `- ${span} 语音：ASR 失败，内容未知`;
    const labels = s.label_status === "ok"
      ? s.labels.slice(0, maxLabels).map((l) => `${l.tag} ${l.score.toFixed(2)}`).join(", ")
      : s.label_status === "failed" ? "打标失败" : "类别无法判定";
    return `- ${span} 非语音：${labels}`;
  };
  if (segs.length <= maxSeg) {
    for (const s of segs) lines.push(render(s));
  } else {
    const headN = Math.floor(maxSeg / 2), tailN = maxSeg - headN;
    for (const s of segs.slice(0, headN)) lines.push(render(s));
    lines.push(`- …（省略 ${segs.length - maxSeg} 段）`);
    for (const s of segs.slice(-tailN)) lines.push(render(s));
  }
  if (d.gaps.length) {
    lines.push(`缺口：`);
    for (const g of d.gaps) lines.push(`- ${g}`);
  }
  if (d.capabilities_available.length) lines.push(`可用能力：${d.capabilities_available.join(", ")}`);
  const failed = Object.entries(d.tools).filter(([, v]) => v !== "ok");
  if (failed.length) lines.push(`工具状态：${failed.map(([k, v]) => `${k} ${v}`).join(", ")}`);
  return [...head, ...lines, `[${tag} end]`].join("\n");
}

/** 服务不可用 / 超时时的显式降级块（铁律 5、§13）：告诉主模型"本次没有时间轴信息"，并给出兜底文本。 */
export function renderUnavailable(reason: string, fallbackText?: string, tag = "mmp:digest"): string {
  const lines = [`[${tag} unavailable]`, `本次未做多模态预检（${reason}），无时间轴与事件类型信息。`];
  if (fallbackText !== undefined) lines.push(`以下为平台转写兜底（未分段、可能把非语音幻觉成语音）：`, fallbackText);
  lines.push(`[${tag} end]`);
  return lines.join("\n");
}
