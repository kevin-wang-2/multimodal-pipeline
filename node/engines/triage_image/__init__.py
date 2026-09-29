"""triage.image：Florence-2 全图描述、短语定位和对象检测。"""

ENGINE = "florence-2-base-ft"
ENGINE_VERSION = "transformers-4.57.6/2026-09-29"
LARGE_ENGINE = "florence-2-large-ft"
LARGE_ENGINE_VERSION = "transformers-4.57.6/2026-09-29"

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "region_dedupe_iou": {"type": "number", "minimum": 0.5, "maximum": 1, "default": 0.8},
        "pdf_dpi": {"type": "integer", "minimum": 72, "maximum": 300, "default": 144},
    },
    "additionalProperties": False,
}

CAPABILITIES = [{
    "id": "triage.image",
    "modal": "image",
    "description": "图片预检：全图详细描述、描述短语定位与对象检测，输出带像素锚点的任务无关视觉索引。",
    "tiers": [{
        "tier": "gpu-fast", "engine": ENGINE, "engine_version": ENGINE_VERSION, "cost": "low",
        "latency_hint": "~0.4-0.8s/surface warm", "vram_mb": 768, "max_concurrency": 1,
    }],
    "input": {"media": "required", "params_schema": PARAMS_SCHEMA},
    "output_schema": "urn:mmp:protocol:1:digest",
}]
