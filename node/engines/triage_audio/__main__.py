"""triage.audio 引擎子进程。启动即加载三件套（hello 在加载之后发），每个 run 跑一条媒体。"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import numpy as np

from engines.common.io import BadParams, log, serve
from engines.triage_audio import ENGINE, ENGINE_VERSION, PARAMS_SCHEMA
from engines.triage_audio.audio import SR, UnsupportedAudio, read_wav_16k
from engines.triage_audio.digest import RawSegment, build_digest, timeline
from engines.triage_audio.vad import SileroVad, VadParams, speech_segments

MODELS = Path(os.environ.get("MMP_MODELS_DIR") or Path(__file__).resolve().parents[3] / "models")
THREADS = int(os.environ.get("MMP_ENGINE_THREADS", "4"))
SENSE_DIR = MODELS / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
TAG_DIR = MODELS / "sherpa-onnx-zipformer-small-audio-tagging-2024-04-15"
VAD_PATH = MODELS / "silero_vad.onnx"
TOP_K = 5


class Models:
    def __init__(self):
        import sherpa_onnx  # 放这里：import 失败要在 hello 之前以非零退出，A 会报 engine_failed

        t0 = time.monotonic()
        self.vad = SileroVad(str(VAD_PATH), num_threads=1)
        self.tagger = sherpa_onnx.AudioTagging(sherpa_onnx.AudioTaggingConfig(
            model=sherpa_onnx.AudioTaggingModelConfig(
                zipformer=sherpa_onnx.OfflineZipformerAudioTaggingModelConfig(model=str(TAG_DIR / "model.int8.onnx")),
                num_threads=THREADS, debug=False),
            labels=str(TAG_DIR / "class_labels_indices.csv"), top_k=TOP_K))
        self.asr = {}
        self._sherpa = sherpa_onnx
        self._warmup()
        self.load_ms = int((time.monotonic() - t0) * 1000)

    def _warmup(self) -> None:
        """onnxruntime 首次推理有几百毫秒的一次性开销（T1 "加载占七成"的同类问题）；用 1s 静音把它吃掉。"""
        silence = np.zeros(SR, dtype=np.float32)
        self.vad.probabilities(silence)
        st = self.tagger.create_stream()
        st.accept_waveform(sample_rate=SR, waveform=silence)
        self.tagger.compute(st)
        rec = self.recognizer("auto")
        st = rec.create_stream()
        st.accept_waveform(SR, silence)
        rec.decode_stream(st)

    def recognizer(self, language: str):
        if language not in self.asr:
            self.asr[language] = self._sherpa.OfflineRecognizer.from_sense_voice(
                model=str(SENSE_DIR / "model.int8.onnx"), tokens=str(SENSE_DIR / "tokens.txt"),
                num_threads=THREADS, use_itn=True, language=language, debug=False)
        return self.asr[language]


MODELS_LOADED: Models | None = None


def _validate_params(params: dict) -> tuple[float, str]:
    allowed = set(PARAMS_SCHEMA["properties"])
    extra = set(params) - allowed
    if extra:
        raise BadParams(f"unknown params {sorted(extra)}")
    th = float(params.get("label_confidence_threshold", 0.30))
    lang = str(params.get("language", "auto"))
    if not 0 <= th <= 1 or lang not in PARAMS_SCHEMA["properties"]["language"]["enum"]:
        raise BadParams("bad param value")
    return th, lang


def triage(m: Models, media_id: str, path: str, params: dict) -> dict:
    th, lang = _validate_params(params)
    timings: dict[str, int] = {}
    tools = {"vad": "ok", "audio_tagging": "ok", "asr": "ok"}

    try:
        x = read_wav_16k(path)
    except UnsupportedAudio as e:
        raise BadParams(f"unsupported media: {e}") from e
    duration = len(x) / SR
    if duration == 0:
        return build_digest(media_id, 0.0, [], {"vad": "skipped", "audio_tagging": "skipped", "asr": "skipped"},
                            th, {}) | {"gaps": ["媒体时长为 0，无任何内容"]}

    # 1) VAD
    t0 = time.monotonic()
    try:
        speech = speech_segments(m.vad.probabilities(x), duration, VadParams())
    except Exception as e:  # VAD 挂了就整条当一段，后面照常打标 / ASR，gaps 里说明
        log(f"vad failed: {e!r}", "error")
        tools["vad"] = "failed"
        speech = []
    timings["vad"] = int((time.monotonic() - t0) * 1000)
    segs = timeline(duration, speech)
    if tools["vad"] == "failed" and segs:
        segs = [RawSegment(0.0, round(duration, 3), True)]

    # 2) 段级打标（每段一次，次数随段数不随时长）
    t0 = time.monotonic()
    for s in segs:
        try:
            st = m.tagger.create_stream()
            st.accept_waveform(sample_rate=SR, waveform=np.ascontiguousarray(x[int(s.start * SR):int(s.end * SR)]))
            s.labels = [{"tag": e.name, "score": float(e.prob)} for e in m.tagger.compute(st)]
        except Exception as e:
            log(f"tagging failed on {s.start}-{s.end}: {e!r}", "error")
            s.tag_failed = True
            tools["audio_tagging"] = "partial"
    if segs and all(s.tag_failed for s in segs):
        tools["audio_tagging"] = "failed"
    timings["tagging"] = int((time.monotonic() - t0) * 1000)

    # 2b) VAD 漏检补救：VAD 判非语音、但打标 Speech ≥ 阈值的段也当语音跑 ASR（结果-T6 样本 #10：一句话被 VAD 整段漏掉，打标 Speech 0.98）
    for s in segs:
        if not s.speech and not s.tag_failed:
            if max((l["score"] for l in s.labels if l["tag"] == "Speech"), default=0.0) >= th:
                s.speech = True
                s.vad_missed = True

    # 3) 只对语音段跑 ASR
    t0 = time.monotonic()
    speech_segs = [s for s in segs if s.speech]
    if speech_segs:
        try:
            rec = m.recognizer(lang)
        except Exception as e:
            log(f"asr load failed: {e!r}", "error")
            rec = None
            tools["asr"] = "failed"
        for s in speech_segs:
            if rec is None:
                s.asr_failed = True
                continue
            try:
                st = rec.create_stream()
                st.accept_waveform(SR, np.ascontiguousarray(x[int(s.start * SR):int(s.end * SR)]))
                rec.decode_stream(st)
                r = st.result
                # SenseVoice 不给置信度；用打标里 Speech 的分数当代理：VAD 说是语音但打标没听到 Speech 的段（喷麦、呼吸）常被幻觉成一两个词
                speech_score = max((l["score"] for l in s.labels if l["tag"] == "Speech"), default=0.0)
                s.asr = {"text": r.text.strip(), "lang": (getattr(r, "lang", "") or "").strip("<>|") or "n/a",
                         "confidence": round(float(speech_score), 3)}
            except Exception as e:
                log(f"asr failed on {s.start}-{s.end}: {e!r}", "error")
                s.asr_failed = True
                tools["asr"] = "partial"
        if rec is not None and speech_segs and all(s.asr_failed for s in speech_segs):
            tools["asr"] = "failed"
    else:
        tools["asr"] = "skipped"
    timings["asr"] = int((time.monotonic() - t0) * 1000)

    return build_digest(media_id, duration, segs, tools, th, timings)


async def run(job: dict, cancelled: asyncio.Event) -> tuple[object, dict]:
    assert MODELS_LOADED is not None
    # 模型推理是同步 CPU 工作；放线程池里跑，让 cancel / 其他 run 的 IO 不被卡住
    digest = await asyncio.to_thread(triage, MODELS_LOADED, job["media_id"], job["media_path"], job.get("params") or {})
    return digest, dict(digest["timings_ms"])


def main() -> None:
    global MODELS_LOADED
    for p in (VAD_PATH, TAG_DIR / "model.int8.onnx", SENSE_DIR / "model.int8.onnx"):
        if not p.exists():
            print(f"model missing: {p} (set MMP_MODELS_DIR)", file=sys.stderr)
            sys.exit(3)
    MODELS_LOADED = Models()
    print(f"models loaded in {MODELS_LOADED.load_ms} ms from {MODELS}", file=sys.stderr)
    serve(ENGINE, ENGINE_VERSION, run)


if __name__ == "__main__":
    main()
