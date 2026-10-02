"""triage.audio：音频预检（架构.md §11）。Silero VAD 定界 → 段级 AudioSet 打标 → 语音段 SenseVoice ASR → 确定性模板组装 digest。

只依赖 onnxruntime + sherpa-onnx + numpy，零 torch。模型目录由环境变量 MMP_MODELS_DIR 指定（默认仓库根的 models/）。
"""

from .presentation import render_agent_context

ENGINE = "silero-vad+sensevoice-int8+zipformer-tagging-int8"
ENGINE_VERSION = "2026-09-21b"         # 模型组合或后处理规则变了就改；进缓存键。21b：asr.confidence=打标 Speech 分数、VAD 漏检由打标补救、不猜原因

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "label_confidence_threshold": {
            "type": "number", "minimum": 0, "maximum": 1, "default": 0.30,
            "description": "段级标签低于此分数 → labels 为空、label_status=unclassified（T4 观察②）",
        },
        "language": {
            "type": "string", "enum": ["auto", "zh", "en", "ja", "ko", "yue"], "default": "auto",
            "description": "SenseVoice 的语言提示",
        },
    },
    "additionalProperties": False,
}

CAPABILITIES = [
    {
        "id": "triage.audio",
        "purpose": "triage",
        "description": "音频预检：VAD 定界 + 段级粗类别打标 + 语音段 ASR，产出带时间轴的 digest（原子事实 + gaps + 可用能力清单）。",
        "tiers": [
            {"tier": "cpu", "engine": ENGINE, "engine_version": ENGINE_VERSION, "cost": "low",
             "latency_hint": "~0.05x realtime on 4 CPU threads", "max_concurrency": 1},
        ],
        "input": {"media": {"presence": "required", "accepts": ["audio/*", "video/*"],
                            "preprocessor": "audio.to_wav_16k_mono"}, "params_schema": PARAMS_SCHEMA},
        "output": {"schema": "urn:mmp:protocol:2:digest", "agent_context": "required"},
    }
]

AGENT_CONTEXT_PRESENTERS = {"triage.audio": render_agent_context}
