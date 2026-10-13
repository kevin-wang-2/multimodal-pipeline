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
    "description": "Krea 2 Turbo FP8 文生图：接受命名 prompt 与 options，8 步生成单张 PNG。",
    "tiers": [{"tier": "gpu", "engine": ENGINE, "engine_version": ENGINE_VERSION,
               "cost": "high", "vram_mb": 12000, "max_concurrency": 1}],
    "input": {"fields": [
        {"name": "prompt", "kind": "text", "required": True,
         "description": "图像生成提示词", "schema": {"type": "string", "minLength": 1, "maxLength": 4000}},
        {"name": "options", "kind": "json", "required": False,
         "description": "尺寸与随机种子", "schema": PARAMS_SCHEMA},
    ]},
    "output": {
        "agent_context": "none",
        "fields": [
          {"name": "image", "kind": "media", "required": True,
           "description": "生成的单张图片", "produces": ["image/png"],
           "cardinality": {"min": 1, "max": 1}},
          {"name": "metadata", "kind": "json", "required": True,
           "description": "生成参数与耗时", "schema": {
            "type": "object",
            "properties": {
                "width": {"type": "integer", "minimum": 1},
                "height": {"type": "integer", "minimum": 1},
                "seed": {"type": "integer", "minimum": 0},
                "timings_ms": {"type": "object"},
            },
            "required": ["width", "height", "seed"],
            "additionalProperties": False,
          }},
        ],
    },
}]
