"""ImageDigest 到通用 agent_context 的确定性展示。"""
from __future__ import annotations


def render_agent_context(digest: dict) -> dict:
    source = digest["source"]
    degraded = source.get("degraded", False)
    degraded_text = source.get("degraded_reason", "yes") if degraded else "no"
    lines = [
        "[mmp:agent-context start]",
        (f"media={digest['media_id']} kind=image format={digest['format']} "
         f"engine={source['engine']} tier={source['tier']} degraded={degraded_text}"),
    ]
    if degraded:
        lines.append(f"⚠ 本次预检降级（{source.get('degraded_reason', 'unknown')}）：结果不完整，按需重新分析。")
    lines.append("像素平面：")
    for surface in digest["surfaces"]:
        lines.append(
            f"- {surface['id']} kind={surface['kind']} index={surface['index']} "
            f"size={surface['width_px']}x{surface['height_px']} rotation={surface['rotation_deg']}deg"
        )
    description = digest.get("description")
    if description:
        tools = ",".join(description["provenance"]["tools"])
        lines.append(f"整体描述（模型推断；tools={tools}）：{description['text']}")
    else:
        lines.append("整体描述：未产出")
    lines.append("语义区域（bbox 为归正后像素坐标 [x_min,y_min,x_max,y_max)）：")
    if digest["regions"]:
        for region in digest["regions"]:
            box = region["bbox_px"]
            tools = ",".join(region["provenance"]["tools"])
            detail = f" description={region['description']}" if region.get("description") else ""
            lines.append(
                f"- {region['id']} surface={region['surface_id']} "
                f"bbox=[{box['x_min']},{box['y_min']},{box['x_max']},{box['y_max']}) "
                f"label={region['label']}{detail} provenance=model_inference tools={tools}"
            )
    else:
        lines.append("- 未产出可用区域")
    signals = [f"{signal['code']}={signal['status']}" +
               (f"({signal['value']})" if "value" in signal else "")
               for signal in digest["quality_signals"]]
    lines.append("质量信号：" + ", ".join(signals))
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
