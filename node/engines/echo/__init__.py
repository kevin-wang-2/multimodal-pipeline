"""echo：S1 用来验证 infra 的任务类型。原样返回媒体 hash、大小与 params；可配置 sleep 与故意失败。"""

ENGINE = "echo"
ENGINE_VERSION = "1"

CAPABILITIES = [
    {
        "id": "echo",
        "purpose": "diagnostic",
        "description": "测试用：返回媒体 hash 与参数，不做任何推理。",
        "tiers": [
            {"tier": "cpu", "engine": ENGINE, "engine_version": ENGINE_VERSION, "cost": "low",
             "latency_hint": "~sleep_ms", "max_concurrency": 4},
        ],
        "input": {
            "media": {"presence": "required", "accepts": ["*/*"]},
            "params_schema": {
                "type": "object",
                "properties": {
                    "sleep_ms": {"type": "integer", "minimum": 0, "maximum": 600000, "default": 0},
                    "fail": {"type": "boolean", "default": False},
                    "pad_bytes": {"type": "integer", "minimum": 0, "default": 0,
                                  "description": "在结果里塞这么多字节的填充，用来测大输出走 put"},
                    "tag": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        "output": {
            "schema": {
                "type": "object",
                "properties": {
                    "media_id": {"type": "string"}, "bytes": {"type": "integer"}, "params": {"type": "object"},
                    "started_at_ms": {"type": "integer"}, "pad": {"type": "string"},
                },
                "required": ["media_id", "bytes", "params", "started_at_ms"],
            },
            "agent_context": "none",
        },
    }
]
