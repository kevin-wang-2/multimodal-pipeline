"""AudioDigest 到通用 agent_context 的确定性展示。"""
from __future__ import annotations


def _fmt(value: float) -> str:
    return f"{value:.2f}"


def render_agent_context(digest: dict) -> dict:
    source = digest["source"]
    degraded = source.get("degraded", False)
    degraded_text = source.get("degraded_reason", "yes") if degraded else "no"
    lines = [
        "[mmp:agent-context start]",
        (f"media={digest['media_id']} kind=audio duration={_fmt(digest['duration_sec'])}s "
         f"engine={source['engine']} tier={source['tier']} degraded={degraded_text}"),
    ]
    if degraded:
        lines.append(f"⚠ 本次预检降级（{source.get('degraded_reason', 'unknown')}）：结果不完整，按需重新分析。")
    threshold = source.get("params", {}).get("label_confidence_threshold", 0.3)
    lines.append("时间轴（秒，[start,end)）：")

    def render(segment: dict) -> str:
        span = f"[{_fmt(segment['start'])}, {_fmt(segment['end'])})"
        asr = segment.get("asr", "missing")
        if isinstance(asr, dict):
            lang = f" ({asr['lang']})" if asr.get("lang") not in (None, "n/a") else ""
            confidence = asr.get("confidence")
            if isinstance(confidence, (int, float)) and confidence < threshold:
                labels = segment.get("labels", [])
                top = f"打标 {labels[0]['tag']} {labels[0]['score']:.2f}" if labels else "打标无明确类别"
                return f"- {span} 语音（低置信 {confidence:.2f}，{top}）：「{asr.get('text', '')}」{lang}"
            return f"- {span} 语音：「{asr.get('text', '')}」{lang}"
        if asr == "missing":
            return f"- {span} 语音：ASR 失败，内容未知"
        if segment["label_status"] == "ok":
            label = ", ".join(f"{item['tag']} {item['score']:.2f}" for item in segment["labels"][:2])
        elif segment["label_status"] == "failed":
            label = "打标失败"
        else:
            label = "类别无法判定"
        return f"- {span} 非语音：{label}"

    segments = digest["segments"]
    if len(segments) <= 40:
        lines.extend(render(segment) for segment in segments)
    else:
        lines.extend(render(segment) for segment in segments[:20])
        lines.append(f"- …（省略 {len(segments) - 40} 段）")
        lines.extend(render(segment) for segment in segments[-20:])
    lines.append("缺口：")
    lines.extend(f"- {gap}" for gap in digest["gaps"])
    if not digest["gaps"]:
        lines.append("- 无")
    capabilities = ", ".join(digest["capabilities_available"]) or "无"
    lines.append("可用能力：" + capabilities)
    lines.append("工具状态：" + ", ".join(f"{name} {status}" for name, status in digest["tools"].items()))
    lines.append("[mmp:agent-context end]")
    return {"format": "mmp-agent-context-v1", "content_type": "text/plain; charset=utf-8",
            "text": "\n".join(lines)}
