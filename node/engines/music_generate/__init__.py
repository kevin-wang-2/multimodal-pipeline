"""YuE2 BF16 music generation; model dependencies live in the engine process."""

ENGINE = "yue2-bf16-cudnn"
RUNTIME_VERSION = "0.1.6"
MODEL_REPOSITORY = "m-a-p/YuE2-3B"
MODEL_REVISION = "8f24312f187763b9854adeec87e437e2b03bbff1"
VAE_REPOSITORY = "m-a-p/YuE2-Vae"
VAE_REVISION = "152733a19ad43aa67e367f9b5503ef8075bb5126"
ENGINE_VERSION = "2-0.1.6-cudnn-8f24312f-152733a1"

EXPECTED_WEIGHTS = {
    "mot": {
        "files": {
            "model.safetensors": {
                "sha256": "1d55c42c1a9875c34f5d736e15078449992b044e807ce2a138e6cf289a1e59e9",
                "bytes": 7261441640
            }
        },
        "config_sha256": "ad3477bbef890bf98ae196c1e4b44779494a6231c4ab66f32708eabadf265329"
    },
    "vae": {
        "files": {
            "model.safetensors": {
                "sha256": "807ce9d5149fa27c5ad3e6582058469852e908f6c5acc8c8aa338e7ab7751346",
                "bytes": 530512720
            }
        },
        "config_sha256": "f0191bb9694009956de44e0c361a6f1334760be4c8f848e599bde242a54a0970"
    }
}

OPTIONS_SCHEMA = {
    "type": "object",
    "properties": {"seed": {"type": "integer", "minimum": 0, "maximum": 2**32 - 1,
                              "default": 831001},
                   "mode": {"type": "string", "enum": ["song", "instrumental"], "default": "song"}},
    "additionalProperties": False,
}
CAPABILITIES = [{
    "id": "music.generate", "visibility": "public", "purpose": "music.generate",
    "description": "YuE2 BF16：song 歌曲与 instrumental 纯音乐生成，48 kHz 双声道 FLAC。纯音乐 lyrics 为可选结构描述；模型规划后将 Vocal 音符转入 Ins，再以固定曲谱生成。大音频需要 delivery.audio PUT 端点。",
    # Reserve the pinned runtime's GPU allocation ceiling plus CUDA workspace,
    # rather than treating one sampled song's peak as every input's upper bound.
    "tiers": [{"tier": "gpu", "engine": ENGINE, "engine_version": ENGINE_VERSION,
               "cost": "high", "vram_mb": 15000, "max_concurrency": 1}],
    "input": {"fields": [
        {"name": "style", "kind": "text", "required": True,
         "description": "音乐风格、语言、乐器、演唱特点与速度",
         "schema": {"type": "string", "minLength": 1, "maxLength": 4000}},
        {"name": "lyrics", "kind": "text", "required": False,
         "description": "song 模式必须提供演唱歌词；instrumental 模式为可选结构描述（段落、发展与编排），不会作为演唱文字。省略时由模型规划结构。",
         "schema": {"type": "string", "minLength": 0, "maxLength": 12000}},
        {"name": "score", "kind": "text", "required": False,
         "description": "可选 YuE2 原生 ABC 曲谱；可将上一轮返回的 score 修改后作为新输入",
         "schema": {"type": "string", "minLength": 1, "maxLength": 50000}},
        {"name": "options", "kind": "json", "required": False,
         "description": "mode 选择 song（默认）或 instrumental；seed 为随机种子。固定 BF16、full 乐谱规划与 32 步。",
         "schema": OPTIONS_SCHEMA},
    ]},
    "output": {"agent_context": "none", "fields": [
        {"name": "audio", "kind": "media", "required": True,
         "description": "生成的 48 kHz 双声道 FLAC", "produces": ["audio/flac"],
         "cardinality": {"min": 1, "max": 1}},
        {"name": "score", "kind": "text", "required": True,
         "description": "生成或使用的 ABC 曲谱", "schema": {"type": "string"}},
        {"name": "source_score", "kind": "text", "required": False,
         "description": "纯音乐模式转换前的模型原谱或用户输入谱，便于与最终 score 对照。",
         "schema": {"type": "string"}},
        {"name": "metadata", "kind": "json", "required": True,
         "description": "音频信息、随机种子、截断状态与固定执行配方",
         "schema": {"type": "object", "properties": {
             "sample_rate": {"const": 48000}, "channels": {"const": 2},
             "duration_sec": {"type": "number", "exclusiveMinimum": 0},
             "seed": {"type": "integer", "minimum": 0},
             "mode": {"type": "string", "enum": ["song", "instrumental"]},
             "conversion": {"type": "object", "description": "纯音乐转谱检查及声部重叠时保留或缩减的器乐音符。"},
             "truncated": {"type": "object", "properties": {
                 "abc": {"type": "boolean"}, "semantic": {"type": "boolean"}},
                 "required": ["abc", "semantic"], "additionalProperties": False},
             "attention": {"const": "cudnn"}, "ar_execution": {"const": "cuda_graph"},
             "model_dtype": {"const": "bfloat16"}, "vae_dtype": {"const": "float32"},
             "ode_steps": {"const": 32}, "timings_ms": {"type": "object"},
         }, "required": ["sample_rate", "channels", "duration_sec", "seed", "truncated",
                         "attention", "ar_execution", "model_dtype", "vae_dtype", "ode_steps", "mode"],
             "additionalProperties": False}},
    ]},
}]
