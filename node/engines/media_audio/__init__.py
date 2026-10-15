"""CPU audio tools. Only schemas are imported by A; FFmpeg runs out of process."""

from engines.common.media_tools import choice, integer, number, options

ENGINE = "media-audio"
ENGINE_VERSION = "1"
FORMAT = {
    "format": choice("wav", "flac", "mp3", "ogg", "opus", "m4a", default="flac"),
    "bitrate_kbps": integer(16, 320),
    "bit_depth": choice("16", "24"),
}


def media(many=False):
    return {"name": "audio", "kind": "media", "required": True,
            "accepts": ["audio/*", "application/ogg", "application/octet-stream"],
            "cardinality": {"min": 1, "max": 16 if many else 1}}


def capability(op, description, properties=None, required=(), many=False):
    fields = [media(many)]
    if properties is not None:
        fields.append({"name": "options", "kind": "json", "required": bool(required),
                       "schema": options({**FORMAT, **properties}, required)})
        if op == "channels":
            fields[-1]["schema"]["allOf"] = [{
                "if": {"properties": {"mode": {"const": "extract"}}},
                "then": {"required": ["channel"]}, "else": {"not": {"required": ["channel"]}},
            }]
        if op == "silence":
            fields[-1]["schema"] = {"type": "object", "oneOf": [
                options({**FORMAT, "mode": {"const": mode}, **{name: properties[name] for name in names}},
                        ("mode", *extra_required))
                for mode, names, extra_required in (
                    ("pad", ("before_sec", "after_sec"), ()),
                    ("mute", ("start_sec", "end_sec"), ("start_sec", "end_sec")),
                    ("trim", ("threshold_db", "min_silence_sec"), ()),
                )
            ]}
    outputs = [{"name": "metadata", "kind": "json", "required": True,
                "schema": {"type": "object", "properties": {
                    "files": {"type": "array", "minItems": 1, "maxItems": 32, "items": {
                        "type": "object", "properties": {
                            "format": {"type": "string"}, "codec": {"type": "string"},
                            "sample_rate": integer(1, 384000), "channels": integer(1, 32),
                            "duration_sec": number(0, 86400), "bytes": integer(0, 9007199254740991),
                            "bit_depth": integer(0, 64),
                        }, "required": ["format", "codec", "sample_rate", "channels", "duration_sec", "bytes"],
                        "additionalProperties": False,
                    }}, "timings_ms": {"type": "object"},
                }, "required": ["files"], "additionalProperties": False}}]
    if op != "inspect":
        outputs.insert(0, {"name": "audio", "kind": "media", "required": True,
            "produces": ["audio/wav", "audio/flac", "audio/mpeg", "audio/ogg", "audio/mp4"],
            "cardinality": {"min": 1, "max": 32 if op == "channels" else 1}})
    return {"id": f"audio.{op}", "purpose": f"audio.{op}", "visibility": "public",
            "description": description + "；不做 AI 推理；不支持、损坏、超限或会削波时明确报错。",
            "tiers": [{"tier": "cpu", "engine": ENGINE, "engine_version": ENGINE_VERSION,
                       "cost": "low", "max_concurrency": 1}],
            "input": {"fields": fields}, "output": {"fields": outputs, "agent_context": "none"}}


CAPABILITIES = [
    capability("inspect", "读取实际音频容器、编码、时长、采样率、声道和位深"),
    capability("convert", "转换音频格式、编码码率或 PCM/FLAC 位深", {}),
    capability("crop", "按秒截取精确起止区间，区间必须完全在音频内", {
        "start_sec": number(0, 86400), "end_sec": number(0, 86400),
    }, ("start_sec", "end_sec")),
    capability("resample", "重采样为指定 Hz，不改变播放速度", {
        "sample_rate": integer(8000, 192000),
    }, ("sample_rate",)),
    capability("channels", "提取零起始声道、按序拆分全部声道、混成单声道或转双声道；总是返回媒体数组", {
        "mode": choice("extract", "split", "mono", "stereo"), "channel": integer(0, 31),
    }, ("mode",)),
    capability("volume", "按 dB 调整音量；默认拒绝削波，limit 显式启用峰值限制", {
        "gain_db": number(-96, 24), "clipping": choice("reject", "limit", default="reject"),
    }, ("gain_db",)),
    capability("normalize", "两遍测量归一化至指定 LUFS 和真峰值；静音或无法线性达到目标则报错", {
        "lufs": number(-36, -5, default=-16), "true_peak_db": number(-9, 0, default=-1),
    }),
    capability("fade", "按秒指定淡入淡出时长；两段不能重叠", {
        "in_sec": number(0, 86400, default=0), "out_sec": number(0, 86400, default=0),
    }),
    capability("concat", "多段依次拼接，可选段间静音或交叉淡化；默认采用首段的采样率与声道", {
        "gap_sec": number(0, 60, default=0), "crossfade_sec": number(0, 60, default=0),
    }, many=True),
    capability("mix", "按偏移和各轨增益叠加音频；默认拒绝削波，可显式 normalize 平均混音", {
        "offsets_sec": {"type": "array", "minItems": 1, "maxItems": 16, "items": number(0, 3600)},
        "gains_db": {"type": "array", "minItems": 1, "maxItems": 16, "items": number(-96, 24)},
        "normalize": {"type": "boolean", "default": False},
    }, many=True),
    capability("silence", "前后加静音、指定区间静音或按阈值裁首尾静音；不剪中间停顿", {
        "mode": choice("pad", "mute", "trim"),
        "before_sec": number(0, 3600, default=0), "after_sec": number(0, 3600, default=0),
        "start_sec": number(0, 86400), "end_sec": number(0, 86400),
        "threshold_db": number(-96, -10, default=-50),
        "min_silence_sec": number(0.001, 10, default=0.1),
    }, ("mode",)),
]
