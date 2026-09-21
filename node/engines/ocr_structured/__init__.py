"""ocr.structured：文档 OCR（结果-T5）。快档 PP-OCRv6 出文本行 + 质量信号；慢档 PaddleOCR-VL 出版面结构（Markdown + 元素标签）。

两档跑在同一个子进程（同一 conda 环境 paddlepaddle-gpu），tier 由任务指定；VRAM 预算按 tier 在注册表声明，调度器据此串行慢档。
升级不在引擎内自动发生：快档按页给 flags 与 suggest_upgrade_pages，上层决定是否再提一个 tier=gpu 的任务（T5 §5.4 只做按需升级）。
"""

ENGINE_FAST = "pp-ocrv6"
ENGINE_VL = "paddleocr-vl"
VERSION_FAST = "paddleocr-3.7.0"
VERSION_VL = "paddleocr-3.7.0/vl-1.6-0.9b"

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "pages": {"type": "array", "items": {"type": "integer", "minimum": 1}, "uniqueItems": True,
                  "description": "1-based 页码；省略 = 全部页"},
        "lang": {"type": "string", "enum": ["ch", "en"], "default": "ch", "description": "PP-OCR 语种模型；VL 与之无关"},
        "dpi": {"type": "integer", "minimum": 72, "maximum": 600, "default": 200, "description": "PDF 栅格化分辨率"},
        "low_conf_threshold": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.7,
                               "description": "行置信度低于此视为低置信行"},
        "low_conf_ratio_max": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.2,
                               "description": "低置信行占比超过此 → flag low_confidence"},
        "coverage_min": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.45,
                         "description": "检测框覆盖的文字墨迹（去框线）占比低于此 → flag coverage_anomaly，即 T5 §2.4 那种静默丢内容的探测器。"
                                        "校准：正立发票 0.56–0.64；旋转 3° 且无字符丢失 0.47"},
    },
    "additionalProperties": False,
}

LINE = {"type": "object", "properties": {"text": {"type": "string"}, "score": {"type": "number"},
        "box": {"type": "array", "items": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}}},
        "required": ["text", "score", "box"]}
ELEMENT = {"type": "object", "properties": {"type": {"type": "string"}, "text": {"type": "string"},
           "box": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}}, "required": ["type", "text"]}

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "page_count": {"type": "integer", "minimum": 0},
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "minimum": 1},
                    "tier": {"type": "string", "enum": ["gpu-fast", "gpu"]},
                    "width": {"type": "integer"}, "height": {"type": "integer"},
                    "text": {"type": "string", "description": "阅读顺序拼接的纯文本；快档来自行、慢档来自 markdown"},
                    "lines": {"type": "array", "items": LINE},
                    "quality": {
                        "type": "object",
                        "properties": {
                            "lines": {"type": "integer"}, "chars": {"type": "integer"},
                            "mean_score": {"type": "number"}, "low_conf_ratio": {"type": "number"},
                            "ink_ratio": {"type": "number", "description": "页面墨迹像素占比"},
                            "coverage": {"type": "number", "description": "被检测框覆盖的墨迹占比；低 = 有字没被认出来"},
                        },
                        "required": ["lines", "chars"],
                    },
                    "flags": {"type": "array", "items": {"type": "string", "enum": ["low_confidence", "coverage_anomaly", "empty"]}},
                    "markdown": {"type": "string"},
                    "elements": {"type": "array", "items": ELEMENT},
                },
                "required": ["page", "tier", "text", "quality", "flags"],
            },
        },
        "suggest_upgrade_pages": {"type": "array", "items": {"type": "integer"},
                                  "description": "快档判定值得上慢档的页（flags 非空）。慢档结果里为空数组"},
        "timings_ms": {"type": "object", "additionalProperties": {"type": "integer"}},
    },
    "required": ["page_count", "pages", "suggest_upgrade_pages"],
}

CAPABILITIES = [
    {
        "id": "ocr.structured",
        "modal": "document",
        "description": "文档 OCR。gpu-fast：PP-OCRv6 文本行 + 每页质量信号（低置信行占比、覆盖率）；gpu：PaddleOCR-VL 版面结构（Markdown、表格、印章等元素标签），只对需要的页用。",
        "tiers": [
            {"tier": "gpu-fast", "engine": ENGINE_FAST, "engine_version": VERSION_FAST, "cost": "low",
             "latency_hint": "~0.5s/page", "vram_mb": 2048, "max_concurrency": 2},
            {"tier": "gpu", "engine": ENGINE_VL, "engine_version": VERSION_VL, "cost": "high",
             "latency_hint": "~9s/page", "vram_mb": 12288, "max_concurrency": 1},
        ],
        "input": {"media": "required", "params_schema": PARAMS_SCHEMA},
        "output_schema": OUTPUT_SCHEMA,
    }
]
