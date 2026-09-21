"""triage.audio：digest 组装规则与 VAD 状态机的单测（不碰模型），以及真模型的端到端（模型 / 素材不在时跳过）。"""
from __future__ import annotations

import base64
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from conftest import NODE_DIR, V_ANY, assert_valid, build_stack, validator

sys.path.insert(0, str(NODE_DIR))
from engines.triage_audio.digest import RawSegment, build_digest, timeline  # noqa: E402
from engines.triage_audio.vad import HOP_SEC, VadParams, speech_segments  # noqa: E402

V_DIGEST = validator("urn:mmp:protocol:1:digest")
MID = "sha256:" + "ab" * 32
SRC = {"tier": "cpu", "engine": "e", "engine_version": "1", "generated_at": "2026-09-20T00:00:00Z", "degraded": False}


def with_source(d: dict) -> dict:
    return {**d, "source": SRC}


# ---------- timeline ----------
def test_timeline_alternates_and_covers_full_duration():
    segs = timeline(10.0, [(2.0, 4.0), (6.0, 8.0)])
    assert [(s.start, s.end, s.speech) for s in segs] == [(0, 2.0, False), (2.0, 4.0, True), (4.0, 6.0, False), (6.0, 8.0, True), (8.0, 10.0, False)]
    # 空隙 < 50ms 并入语音段；结尾不足 50ms 并入最后一段
    segs = timeline(4.03, [(0.02, 2.0), (2.03, 4.0)])
    assert [(s.start, s.end, s.speech) for s in segs] == [(0.0, 2.0, True), (2.0, 4.03, True)]
    assert timeline(3.0, []) == [RawSegment(0.0, 3.0, False)]
    assert timeline(0.0, []) == []


# ---------- build_digest 规则 ----------
def test_digest_low_confidence_is_unclassified_and_in_gaps():
    segs = [RawSegment(0.0, 2.0, False, labels=[{"tag": "Boing", "score": 0.118}]),
            RawSegment(2.0, 4.0, True, labels=[{"tag": "Speech", "score": 0.7}], asr={"text": "你好", "lang": "zh"})]
    d = build_digest(MID, 4.0, segs, {"vad": "ok", "audio_tagging": "ok", "asr": "ok"}, 0.30, ["x"], {"vad": 1})
    assert_valid(V_DIGEST, with_source(d))
    assert d["segments"][0]["labels"] == [] and d["segments"][0]["label_status"] == "unclassified"
    assert d["segments"][0]["asr"] is None
    assert d["segments"][1]["label_status"] == "ok" and d["segments"][1]["asr"]["confidence"] == "n/a"
    assert any("0.118 < 阈值 0.30" in g for g in d["gaps"])
    assert any(g.startswith("结构事实") for g in d["gaps"])
    assert d["global"]["speech_ratio"] == 0.5 and "caption" not in d["global"]


def test_digest_classified_nonspeech_and_no_structure_fact_when_speech_first():
    segs = [RawSegment(0.0, 2.0, True, labels=[{"tag": "Speech", "score": 0.9}], asr={"text": "a", "lang": "en"}),
            RawSegment(2.0, 4.0, False, labels=[{"tag": "Music", "score": 0.8}, {"tag": "Synthesizer", "score": 0.6}, {"tag": "Dog", "score": 0.1}])]
    d = build_digest(MID, 4.0, segs, {"vad": "ok", "audio_tagging": "ok", "asr": "ok"}, 0.30, [], {})
    assert_valid(V_DIGEST, with_source(d))
    assert [l["tag"] for l in d["segments"][1]["labels"]] == ["Music", "Synthesizer"]  # Dog 被阈值过滤
    assert any("Music、Synthesizer" in g and "只知类别" in g for g in d["gaps"])
    assert not any(g.startswith("结构事实") for g in d["gaps"])


def test_digest_asr_failure_omits_field_and_marks_gap():
    segs = [RawSegment(0.0, 2.0, True, labels=[{"tag": "Speech", "score": 0.9}], asr=None, asr_failed=True)]
    d = build_digest(MID, 2.0, segs, {"vad": "ok", "audio_tagging": "ok", "asr": "failed"}, 0.30, [], {})
    assert_valid(V_DIGEST, with_source(d))
    assert "asr" not in d["segments"][0]              # 省略 = 取不到；不是 null
    assert any("ASR 失败" in g for g in d["gaps"]) and any("工具 asr 状态 failed" in g for g in d["gaps"])


def test_digest_tag_failure():
    segs = [RawSegment(0.0, 2.0, False, tag_failed=True)]
    d = build_digest(MID, 2.0, segs, {"vad": "ok", "audio_tagging": "failed", "asr": "skipped"}, 0.30, [], {})
    assert_valid(V_DIGEST, with_source(d))
    assert d["segments"][0]["label_status"] == "failed" and d["segments"][0]["labels"] == []


# ---------- VAD 状态机 ----------
def test_vad_hysteresis_onset_low_offset_high():
    p = VadParams(on_threshold=0.2, off_threshold=0.5, min_speech_sec=0.1, min_silence_sec=0.1, onset_pad_sec=0)
    probs = np.array([0.0] * 10 + [0.3, 0.4] + [0.9] * 10 + [0.45] * 2 + [0.9] * 5 + [0.1] * 10, dtype=np.float32)
    segs = speech_segments(probs, len(probs) * HOP_SEC, p)
    # 起点在第一个 ≥0.2 的帧（idx 10）；0.45 的两帧不够 min_silence → 不断开；终点在最后一个 ≥0.5 之后
    assert len(segs) == 1
    assert segs[0][0] == pytest.approx(10 * HOP_SEC, abs=1e-6)
    assert segs[0][1] == pytest.approx(29 * HOP_SEC, abs=1e-6)
    # min_speech 过滤掉短毛刺
    probs = np.array([0.0] * 5 + [0.9] * 2 + [0.0] * 10, dtype=np.float32)
    assert speech_segments(probs, len(probs) * HOP_SEC, p) == []
    # 起点回补一帧但不早于 0
    p2 = VadParams(onset_pad_sec=HOP_SEC, min_speech_sec=0.05, min_silence_sec=0.05)
    probs = np.array([0.9] * 8 + [0.0] * 8, dtype=np.float32)
    assert speech_segments(probs, len(probs) * HOP_SEC, p2)[0][0] == 0.0


# ---------- 真模型 ----------
MODELS_DIR = Path(os.environ.get("MMP_MODELS_DIR") or NODE_DIR.parent / "models")
TESTDATA = Path(os.environ.get("MMP_SMOKE_WAVS") or NODE_DIR.parent / "testdata")
ENGINE_PY = os.environ.get("MMP_ENGINE_PYTHON") or sys.executable


def _engine_ready() -> bool:
    if not (MODELS_DIR / "silero_vad.onnx").exists() or not (TESTDATA / "m0_hum_then_speech.wav").exists():
        return False
    return subprocess.run([ENGINE_PY, "-c", "import sherpa_onnx, onnxruntime"], capture_output=True).returncode == 0


real_model = pytest.mark.skipif(not _engine_ready(), reason="需要 models/、testdata/ 与装了 sherpa-onnx 的引擎解释器")


@real_model
async def test_triage_audio_end_to_end(tmp_path):
    s = await build_stack(tmp_path, engines={"triage_audio": {"module": "engines.triage_audio", "timeout_sec": 120, "python": ENGINE_PY,
                                                              "env": {"MMP_MODELS_DIR": str(MODELS_DIR)}}},
                          scheduler__engine_start_timeout_sec=300)
    try:
        wav = (TESTDATA / "m0_hum_then_speech.wav").read_bytes()
        r = await s.submit(type="triage.audio", media={"inline": base64.b64encode(wav).decode()}, wait=120)
        assert r.status_code == 200, r.json()
        d = r.json()["result"]
        assert_valid(V_DIGEST, d)
        assert len(d["segments"]) == 2 and d["segments"][0]["asr"] is None
        cut = d["segments"][1]["start"]
        assert abs(cut - 2.0) <= 0.05, f"VAD cut {cut}"
        assert "谱子" in d["segments"][1]["asr"]["text"]
        assert len(d["gaps"]) == 2 and d["gaps"][1].startswith("结构事实")
        assert d["source"]["engine_version"] and d["source"]["degraded"] is False
        # params 进缓存键；不同阈值 → 重算
        r2 = await s.submit(type="triage.audio", media={"inline": base64.b64encode(wav).decode()}, params={"label_confidence_threshold": 0.9}, wait=120)
        assert r2.status_code == 200 and r2.json()["cached"] is False
        assert r2.json()["result"]["segments"][0]["label_status"] == "unclassified"
        # 非 WAV → bad_request，不是 500
        r3 = await s.submit(type="triage.audio", media={"inline": base64.b64encode(b"ID3\x03\x00garbage" * 50).decode()}, wait=60)
        assert r3.status_code == 400 and r3.json()["error"] == "bad_request"
    finally:
        await s.client.aclose(); await s.link.close(); await s.node.close()


# ---------- 真实音频样本 #1 暴露的两条规则 ----------
def test_vad_does_not_merge_by_default_and_short_gap_is_surfaced():
    """一句话被 230ms 的风噪切成两半：默认不合并（合并会抹掉夹在中间的短事件，如一声口哨）；digest 的 gaps 把它点出来。"""
    p = VadParams(on_threshold=0.2, off_threshold=0.5, min_speech_sec=0.1, min_silence_sec=0.2, onset_pad_sec=0)
    probs = np.array([0.9] * 30 + [0.1] * 8 + [0.9] * 30, dtype=np.float32)          # 8 帧 = 256ms 空隙
    assert len(speech_segments(probs, len(probs) * HOP_SEC, p)) == 2
    assert len(speech_segments(probs, len(probs) * HOP_SEC, VadParams(**{**p.__dict__, "merge_gap_sec": 0.5}))) == 1   # 显式打开才合并
    segs = [RawSegment(0.0, 1.0, True, labels=[{"tag": "Speech", "score": 0.99}], asr={"text": "听听我的绝对音感准不准", "lang": "zh", "confidence": 0.99}),
            RawSegment(1.0, 1.3, False, labels=[{"tag": "Whistling", "score": 0.8}]),
            RawSegment(1.3, 2.0, True, labels=[{"tag": "Speech", "score": 0.99}], asr={"text": "这个是A吧", "lang": "zh", "confidence": 0.99})]
    d = build_digest(MID, 2.0, segs, {"vad": "ok", "audio_tagging": "ok", "asr": "ok"}, 0.30, [], {})
    assert_valid(V_DIGEST, with_source(d))
    assert len(d["segments"]) == 3 and d["segments"][1]["labels"][0]["tag"] == "Whistling"   # 口哨没被抹掉
    assert any("300ms，Whistling）夹在两段语音之间" in g for g in d["gaps"])


def test_low_speech_confidence_asr_is_flagged_in_gaps():
    """喷麦被 VAD 判为语音、SenseVoice 幻觉出 "Yeah."：asr.confidence 用打标 Speech 分数，低于阈值进 gaps。"""
    segs = [RawSegment(0.61, 1.09, True, labels=[{"tag": "Sound effect", "score": 0.345}], asr={"text": "Yeah.", "lang": "en", "confidence": 0.0}),
            RawSegment(1.09, 4.0, True, labels=[{"tag": "Speech", "score": 0.996}], asr={"text": "帮我", "lang": "zh", "confidence": 0.996})]
    d = build_digest(MID, 4.0, segs, {"vad": "ok", "audio_tagging": "ok", "asr": "ok"}, 0.30, [], {})
    assert_valid(V_DIGEST, with_source(d))
    assert d["segments"][0]["asr"]["confidence"] == 0.0 and d["segments"][1]["asr"]["confidence"] == 0.996
    assert any("置信度低" in g and "Yeah." in g and "段长 480ms" in g for g in d["gaps"])
    assert not any("喷麦" in g for g in d["gaps"])          # 不猜原因
    assert not any("帮我" in g for g in d["gaps"])


def test_vad_missed_speech_recovered_by_tagger_is_marked():
    segs = [RawSegment(3.39, 5.12, True, labels=[{"tag": "Speech", "score": 0.99}], asr={"text": "识别一下这首歌", "lang": "zh", "confidence": 0.99}),
            RawSegment(5.12, 6.85, True, labels=[{"tag": "Speech", "score": 0.98}], asr={"text": "是什么歌", "lang": "zh", "confidence": 0.98}, vad_missed=True)]
    d = build_digest(MID, 6.85, segs, {"vad": "ok", "audio_tagging": "ok", "asr": "ok"}, 0.30, [], {})
    assert_valid(V_DIGEST, with_source(d))
    assert d["segments"][1]["asr"]["text"] == "是什么歌"
    assert any("VAD 未判为语音，但打标 Speech 0.98" in g and "已补跑 ASR" in g for g in d["gaps"])
