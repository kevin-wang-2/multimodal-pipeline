/**
 * "降级必须显式"（架构 §13）：OpenClaw 的 tools.media 在 mmp-triage 失败时自动回落到平台 STT，
 * 那时 prompt 里有 [Audio] 块却没有我们的边界标记。这个钩子无状态地补上一行说明。
 */
export const DIGEST_MARK = "[mmp:digest start]";
export const UNAVAILABLE_MARK = "[mmp:digest unavailable]";

// OpenClaw 按魔数分类：飞书 / 微信的 voice.m4a（ftyp 容器）会被判成 video/mp4，所以 [Video] 块也算
const AUDIO_BLOCK = /\[(?:Audio|Video)(?:\s+\d+\/\d+)?\]/;

// 媒体理解两级都失败时 prompt 里没有任何块，只剩附件引用（OpenClaw 的 media://inbound/… 或文件名）
const MEDIA_REF = /media:\/\/inbound\/|\b[\w.-]+\.(?:m4a|mp4|mp3|wav|ogg|opus|amr|flac|webm|aac)\b/i;

export type UnavailableKind = "fallback-transcript" | "no-transcript" | null;

/** 需要哪种降级说明：块存在但不是我们的 → 平台兜底；连块都没有但有媒体引用 → 两级都失败。 */
export function unavailableKind(prompt: string): UnavailableKind {
  if (prompt.includes(DIGEST_MARK) || prompt.includes(UNAVAILABLE_MARK)) return null;
  if (AUDIO_BLOCK.test(prompt)) return "fallback-transcript";
  if (MEDIA_REF.test(prompt)) return "no-transcript";
  return null;
}

export function needsUnavailableNote(prompt: string): boolean {
  return unavailableKind(prompt) !== null;
}

export function unavailableNote(kind: UnavailableKind = "fallback-transcript"): string {
  if (kind === "no-transcript") {
    return "⚠ 本次多模态预检不可用，平台转写也没有产出：这条音频 / 视频附件目前没有任何文字信息（无时间轴、无内容）。需要内容时告诉用户预检暂不可用，或用你自己的工具处理该文件。";
  }
  return "⚠ 本次多模态预检不可用：下面 [Audio]/[Video] 块里的转写来自平台兜底，没有时间轴、事件类型和缺口信息，非语音段可能被幻觉成语音。";
}
