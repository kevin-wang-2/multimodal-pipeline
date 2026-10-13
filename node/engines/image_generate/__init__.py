"""Krea 2 Turbo FP8：文本媒体输入，单张 PNG 输出。模型只在引擎子进程加载。"""

ENGINE = "krea2-turbo-fp8"
COMFY_REVISION = "06c67806bcb36789a1e8e1278c6a764b389ff858"
MODEL_REVISION = "eb1eddd3983a54678545a9b2c178c5853b30f7be"
ENGINE_VERSION = "1-06c67806-eb1eddd3"

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "width": {"type": "integer", "minimum": 512, "maximum": 1536,
                  "multipleOf": 64, "default": 1024},
        "height": {"type": "integer", "minimum": 512, "maximum": 1536,
                   "multipleOf": 64, "default": 1024},
        "seed": {"type": "integer", "minimum": 0, "maximum": 4294967295, "default": 42},
    },
    "additionalProperties": False,
}

CAPABILITIES = [{
    "id": "image.generate",
    "visibility": "public",
    "purpose": "image.generate",
    "description": "Krea 2 Turbo FP8 文生图：media 为 UTF-8 text/plain 提示词；8 步生成单张 PNG，"
                   "最多 1048576 像素。结果含 PNG base64；大结果需提供 media.put（接收 JSON）。",
    "tiers": [{"tier": "gpu", "engine": ENGINE, "engine_version": ENGINE_VERSION,
               "cost": "high", "vram_mb": 12000, "max_concurrency": 1}],
    "input": {"media": {"presence": "required", "accepts": ["text/plain"]},
              "params_schema": PARAMS_SCHEMA},
    "output": {
        "agent_context": "none",
        "schema": {
            "type": "object",
            "properties": {
                "content_type": {"const": "image/png"},
                "data_base64": {"type": "string", "minLength": 4},
                "width": {"type": "integer", "minimum": 1},
                "height": {"type": "integer", "minimum": 1},
                "seed": {"type": "integer", "minimum": 0},
                "timings_ms": {"type": "object"},
            },
            "required": ["content_type", "data_base64", "width", "height", "seed"],
            "additionalProperties": False,
        },
    },
}]
