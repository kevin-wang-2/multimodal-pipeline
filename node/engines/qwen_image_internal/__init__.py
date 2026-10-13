"""Qwen Image 2.1 Turbo Int8 internal generation and instruction editing."""

ENGINE = "qwen-image-2.1-turbo-int8"
COMFY_REVISION = "8010b48d2c1be986613f8e8c761527d205bd756f"
MODEL_REPOSITORY = "Comfy-Org/Qwen-Image-2.1"
MODEL_REVISION = "df94239739eef7205973a145ffa6f441f7b64e84"
ENGINE_VERSION = "1-8010b48d-df942397"

DIFFUSION_MODEL = "qwen_image_2.1_turbo_int8_convrot.safetensors"
TEXT_ENCODER = "qwen3vl_8b_int8_convrot.safetensors"
VAE_MODEL = "qwen_image_2.1_vae_bf16.safetensors"

GENERATE_OPTIONS_SCHEMA = {
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

EDIT_OPTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "resolution": {"type": "integer", "minimum": 512, "maximum": 1024,
                       "multipleOf": 32, "default": 1024},
        "seed": {"type": "integer", "minimum": 0, "maximum": 4294967295, "default": 42},
    },
    "additionalProperties": False,
}


def _output() -> dict:
    return {
        "agent_context": "none",
        "fields": [
            {"name": "image", "kind": "media", "required": True,
             "description": "生成或编辑后的单张 PNG；响应 media_id 可直接作为下一轮 image.ref",
             "produces": ["image/png"], "cardinality": {"min": 1, "max": 1}},
            {"name": "metadata", "kind": "json", "required": True,
             "description": "实际输出尺寸、随机种子与分阶段耗时", "schema": {
                 "type": "object",
                 "properties": {
                     "mode": {"type": "string", "enum": ["generate", "edit"]},
                     "width": {"type": "integer", "minimum": 1},
                     "height": {"type": "integer", "minimum": 1},
                     "seed": {"type": "integer", "minimum": 0},
                     "timings_ms": {"type": "object", "additionalProperties": {"type": "integer"}},
                 },
                 "required": ["mode", "width", "height", "seed", "timings_ms"],
                 "additionalProperties": False,
             }},
        ],
    }


_TIER = {"tier": "gpu", "engine": ENGINE, "engine_version": ENGINE_VERSION,
         "cost": "high", "vram_mb": 11000, "max_concurrency": 1}
_PROMPT = {"name": "prompt", "kind": "text", "required": True,
           "description": "生成或编辑指令", "schema": {
               "type": "string", "minLength": 1, "maxLength": 4000}}

CAPABILITIES = [
    {
        "id": "image.generate.qwen",
        "visibility": "internal",
        "purpose": "image.generate",
        "description": "仅限内部非商用的 Qwen Image 2.1 Turbo Int8 文生图；支持指定宽高。",
        "tiers": [dict(_TIER)],
        "input": {"fields": [
            dict(_PROMPT),
            {"name": "options", "kind": "json", "required": False,
             "description": "尺寸与随机种子", "schema": GENERATE_OPTIONS_SCHEMA},
        ]},
        "output": _output(),
    },
    {
        "id": "image.edit.qwen",
        "visibility": "internal",
        "purpose": "image.edit",
        "description": "仅限内部非商用的 Qwen Image 2.1 Turbo Int8 指令编辑；输出可按 media_id 连续编辑。",
        "consumes": {"granularity": "whole"},
        "tiers": [dict(_TIER)],
        "input": {"fields": [
            {"name": "image", "kind": "media", "required": True,
             "description": "要修改的输入图片", "accepts": ["image/*"],
             "cardinality": {"min": 1, "max": 1}},
            dict(_PROMPT),
            {"name": "options", "kind": "json", "required": False,
             "description": "约束总像素规模的分辨率与随机种子", "schema": EDIT_OPTIONS_SCHEMA},
        ]},
        "output": _output(),
    },
]
