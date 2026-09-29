/**
 * "降级必须显式"（架构 §13）：OpenClaw 的 tools.media 在 mmp-triage 失败时自动回落到平台 STT，
 * 那时 prompt 里有 [Audio] 块却没有我们的边界标记。这个钩子无状态地补上一行说明。
 */
export const CONTEXT_MARK = "[mmp:agent-context start]";
export const UNAVAILABLE_MARK = "[mmp:agent-context unavailable]";

// OpenClaw 按魔数分类：飞书 / 微信的 voice.m4a（ftyp 容器）会被判成 video/mp4，所以 [Video] 块也算
const MEDIA_BLOCK = /\[(?:Audio|Video|Image|Document)(?:\s+\d+\/\d+)?\]/;

// 媒体理解两级都失败时 prompt 里没有任何块，只剩附件引用（OpenClaw 的 media://inbound/… 或文件名）
const MEDIA_REF = /media:\/\/inbound\/|\b[\w.-]+\.(?:m4a|mp4|mp3|wav|ogg|opus|amr|flac|webm|aac|jpg|jpeg|png|webp|gif|tif|tiff|pdf)\b/i;

export type UnavailableKind = "fallback-transcript" | "no-transcript" | null;

/** 需要哪种降级说明：块存在但不是我们的 → 平台兜底；连块都没有但有媒体引用 → 两级都失败。 */
export function unavailableKind(prompt: string): UnavailableKind {
  if (prompt.includes(CONTEXT_MARK) || prompt.includes(UNAVAILABLE_MARK)) return null;
  if (MEDIA_BLOCK.test(prompt)) return "fallback-transcript";
  if (MEDIA_REF.test(prompt)) return "no-transcript";
  return null;
}

export function needsUnavailableNote(prompt: string): boolean {
  return unavailableKind(prompt) !== null;
}

export function unavailableNote(kind: UnavailableKind = "fallback-transcript"): string {
  if (kind === "no-transcript") {
    return "⚠ 本次多模态预检不可用，平台也没有产出可用内容：这份附件目前没有预检信息。需要内容时告诉用户预检暂不可用，或用你自己的工具处理该文件。";
  }
  return "⚠ 本次多模态预检不可用：下面的媒体内容来自平台兜底，不包含 MMP 的锨点、工具状态和显式缺口信息。";
}
