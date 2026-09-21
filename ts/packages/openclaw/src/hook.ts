/**
 * "降级必须显式"（架构 §13）：OpenClaw 的 tools.media 在 mmp-triage 失败时自动回落到平台 STT，
 * 那时 prompt 里有 [Audio] 块却没有我们的边界标记。这个钩子无状态地补上一行说明。
 */
export const DIGEST_MARK = "[mmp:digest start]";
export const UNAVAILABLE_MARK = "[mmp:digest unavailable]";

// OpenClaw 按魔数分类：飞书 / 微信的 voice.m4a（ftyp 容器）会被判成 video/mp4，所以 [Video] 块也算
const AUDIO_BLOCK = /\[(?:Audio|Video)(?:\s+\d+\/\d+)?\]/;

export function needsUnavailableNote(prompt: string): boolean {
  return AUDIO_BLOCK.test(prompt) && !prompt.includes(DIGEST_MARK) && !prompt.includes(UNAVAILABLE_MARK);
}

export function unavailableNote(): string {
  return "⚠ 本次多模态预检不可用：下面 [Audio]/[Video] 块里的内容来自平台兜底（或为空），没有时间轴、事件类型和缺口信息，非语音段可能被幻觉成语音。";
}
