"""Florence 原始输出到 ImageDigest 的确定性组装与质量门禁。"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class RawRegion:
    surface_id: str
    label: str
    box: list[float]
    tool: str


def _box(raw: list[float], width: int, height: int) -> dict | None:
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        x1, y1, x2, y2 = (round(float(v)) for v in raw)
    except (TypeError, ValueError):
        return None
    x1, x2 = max(0, min(width - 1, x1)), max(1, min(width, x2))
    y1, y2 = max(0, min(height - 1, y1)), max(1, min(height, y2))
    if x2 <= x1 or y2 <= y1:
        return None
    return {"x_min": x1, "y_min": y1, "x_max": x2, "y_max": y2}


def _iou(a: dict, b: dict) -> float:
    x1, y1 = max(a["x_min"], b["x_min"]), max(a["y_min"], b["y_min"])
    x2, y2 = min(a["x_max"], b["x_max"]), min(a["y_max"], b["y_max"])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    aa = (a["x_max"] - a["x_min"]) * (a["y_max"] - a["y_min"])
    bb = (b["x_max"] - b["x_min"]) * (b["y_max"] - b["y_min"])
    return inter / max(1, aa + bb - inter)


def build_digest(media_id: str, media_format: str, surfaces: list[dict], captions: list[str],
                 raw_regions: list[RawRegion], malformed_boxes: int, dedupe_iou: float,
                 timings: dict[str, int]) -> tuple[dict, str | None]:
    dims = {s["id"]: (s["width_px"], s["height_px"]) for s in surfaces}
    normalized: list[dict] = []
    duplicate_count = 0
    full_count = 0
    for raw in raw_regions:
        width, height = dims[raw.surface_id]
        box = _box(raw.box, width, height)
        if box is None:
            malformed_boxes += 1
            continue
        area = (box["x_max"] - box["x_min"]) * (box["y_max"] - box["y_min"])
        if area / (width * height) >= 0.95:
            full_count += 1
        label = str(raw.label or "object").strip() or "object"
        duplicate = next((r for r in normalized if r["surface_id"] == raw.surface_id
                          and r["label"].casefold() == label.casefold()
                          and _iou(r["bbox_px"], box) >= dedupe_iou), None)
        if duplicate:
            duplicate_count += 1
            tools = duplicate["provenance"]["tools"]
            if raw.tool not in tools:
                tools.append(raw.tool)
            continue
        normalized.append({"surface_id": raw.surface_id, "bbox_px": box, "label": label,
                           "provenance": {"kind": "model_inference", "tools": [raw.tool]}})
    for i, region in enumerate(normalized):
        region["id"] = f"region_{i}"

    text = "\n".join(f"第{i + 1}页：{c}" if len(captions) > 1 else c
                     for i, c in enumerate(captions) if c.strip()).strip()
    grounding_count = sum(1 for r in raw_regions if r.tool == "phrase_grounding")
    raw_count = len(raw_regions)
    duplicate_ratio = duplicate_count / raw_count if raw_count else 0.0
    full_ratio = full_count / raw_count if raw_count else 0.0
    signals = [
        {"code": "description_nonempty", "status": "ok" if text else "failed", "value": bool(text)},
        {"code": "phrase_grounding_present", "status": "ok" if grounding_count else "failed",
         "value": grounding_count > 0},
        {"code": "boxes_well_formed", "status": "ok" if not malformed_boxes else "failed",
         "value": malformed_boxes == 0},
        {"code": "duplicate_box_ratio", "status": "failed" if raw_count >= 4 and duplicate_ratio >= 0.5
         else ("warning" if duplicate_count else "ok"), "value": round(duplicate_ratio, 3)},
        {"code": "full_image_box_ratio", "status": "failed" if raw_count >= 2 and full_ratio >= 0.5
         else ("warning" if full_count else "ok"), "value": round(full_ratio, 3)},
    ]
    failed = [s["code"] for s in signals if s["status"] == "failed"]
    reason = failed[0] if failed else None
    haystack = (text + " " + " ".join(r["label"] for r in normalized)).casefold()
    has_text = bool(re.search(r"text|document|invoice|receipt|contract|sign|label|文字|文档|发票|合同|招牌", haystack))
    tools = {"metadata": "ok", "visual_caption": "ok" if text else "failed",
             "phrase_grounding": "ok" if grounding_count else "failed",
             "object_detection": "ok" if any(r.tool == "object_detection" for r in raw_regions) else "skipped"}
    gaps = []
    if has_text:
        gaps.append("图片中的文字内容未读取")
    if not text:
        gaps.append("视觉描述失败，图片内容未知")
    digest = {"media_id": media_id, "kind": "image", "format": media_format, "surfaces": surfaces,
              "regions": normalized, "quality_signals": signals, "tools": tools, "gaps": gaps,
              "capabilities_available": ["ocr.structured"], "timings_ms": timings}
    if text:
        digest["description"] = {"text": text, "provenance": {"kind": "model_inference", "tools": ["visual_caption"]}}
    return digest, reason
