#!/usr/bin/env python3
"""T4 / M0: 手工串「VAD 定界 → 段级打标 → 语音段 ASR」并组装成 PRD §4.3 的 digest。

这一步的目的是**把 digest schema 定死**：真跑一遍才知道缺什么字段。

用法: .venv/bin/python tools/t4_build_digest.py [wav]
"""
import hashlib, json, os, sys, time, wave
import numpy as np
import sherpa_onnx

SR = 16000
SENSE_DIR = "models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
TAG_DIR = "models/sherpa-onnx-zipformer-small-audio-tagging-2024-04-15"
VAD_PATH = "models/silero_vad.onnx"

# 预检能提供哪些后续能力（任务无关的"能力清单"）
CAPABILITIES = ["pitch_transcribe", "chord_recognize", "source_separate",
                "denoise", "speaker_id", "music_id", "transcribe_segment"]


def read_wav(p):
    with wave.open(p) as w:
        assert w.getframerate() == SR and w.getnchannels() == 1
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def main():
    wav = sys.argv[1] if len(sys.argv) > 1 else "testdata/m0_hum_then_speech.wav"
    samples = read_wav(wav)
    dur = len(samples) / SR
    media_id = "sha256:" + hashlib.sha256(open(wav, "rb").read()).hexdigest()[:16]
    tools = {}

    # ---- 1) VAD 定界 ----
    t0 = time.time()
    vcfg = sherpa_onnx.VadModelConfig()
    vcfg.silero_vad.model = VAD_PATH
    vcfg.silero_vad.min_silence_duration = 0.25
    vcfg.silero_vad.min_speech_duration = 0.25
    vcfg.sample_rate = SR
    vad = sherpa_onnx.VoiceActivityDetector(vcfg, buffer_size_in_seconds=30)
    for i in range(0, len(samples), 512):
        vad.accept_waveform(samples[i:i + 512])
    vad.flush()
    speech_segs = []
    while not vad.empty():
        s = vad.front
        speech_segs.append((s.start / SR, s.start / SR + len(s.samples) / SR, np.array(s.samples)))
        vad.pop()
    t_vad = time.time() - t0
    tools["vad"] = "ok"

    # 按语音段把整条时间轴切成「语音 / 非语音」交替的 segments
    segs = []
    cur = 0.0
    for st, en, sam in speech_segs:
        if st - cur > 0.05:
            segs.append({"start": round(cur, 3), "end": round(st, 3), "kind": "non-speech", "samples": samples[int(cur * SR):int(st * SR)]})
        segs.append({"start": round(st, 3), "end": round(en, 3), "kind": "speech", "samples": sam})
        cur = en
    if cur < dur - 0.05:
        segs.append({"start": round(cur, 3), "end": round(dur, 3), "kind": "non-speech", "samples": samples[int(cur * SR):]})

    # ---- 2) 段级打标 ----
    t0 = time.time()
    tagger = sherpa_onnx.AudioTagging(sherpa_onnx.AudioTaggingConfig(
        model=sherpa_onnx.AudioTaggingModelConfig(
            zipformer=sherpa_onnx.OfflineZipformerAudioTaggingModelConfig(model=os.path.join(TAG_DIR, "model.int8.onnx")),
            num_threads=4, debug=False),
        labels=os.path.join(TAG_DIR, "class_labels_indices.csv"), top_k=5))
    for s in segs:
        st = tagger.create_stream()
        st.accept_waveform(sample_rate=SR, waveform=np.ascontiguousarray(s["samples"], dtype=np.float32))
        s["labels"] = [{"tag": e.name, "score": round(float(e.prob), 3)} for e in tagger.compute(st)][:3]
    t_tag = time.time() - t0
    tools["audio_tagging"] = "ok"

    # ---- 3) 语音段 ASR ----
    t0 = time.time()
    rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=os.path.join(SENSE_DIR, "model.int8.onnx"),
        tokens=os.path.join(SENSE_DIR, "tokens.txt"),
        num_threads=4, use_itn=True, language="zh", debug=False)
    for s in segs:
        if s["kind"] != "speech":
            s["asr"] = None
            continue
        st = rec.create_stream()
        st.accept_waveform(SR, np.ascontiguousarray(s["samples"], dtype=np.float32))
        rec.decode_stream(st)
        r = st.result
        s["asr"] = {"text": r.text.strip(), "lang": (getattr(r, "lang", "") or "").strip("<>|"),
                    "confidence": None}
    t_asr = time.time() - t0
    tools["asr"] = "ok"

    # ---- 4) 组装 digest ----
    for s in segs:
        s.pop("samples", None)
        s.pop("kind", None)
    speech_dur = sum(x["end"] - x["start"] for x in segs if x["asr"])
    gaps = []
    for s in segs:
        if not s["asr"] and s["labels"]:
            top = s["labels"][0]["tag"]
            gaps.append(f"{s['start']:.2f}–{s['end']:.2f}s 是非语音（top={top}:{s['labels'][0]['score']}），"
                        f"只知类别、不知内容（无音高/谱面/歌词）")
    if segs and not segs[0]["asr"]:
        gaps.append(f"注意：检测到「非语音段在前、指令在后的语音段」，"
                    f"用户意图可能指向前面那段（{segs[0]['start']:.2f}–{segs[0]['end']:.2f}s）")

    digest = {
        "media_id": media_id,
        "kind": "audio",
        "duration_sec": round(dur, 3),
        "timeline_unit": "sec",
        "segments": segs,
        "global": {"caption": None, "speech_ratio": round(speech_dur / dur, 3)},
        "tools": tools,
        "gaps": gaps,
        "capabilities_available": CAPABILITIES,
        "source": {"tier": "L1-floor", "engine": "silero-vad+sensevoice-int8+zipformer-tagging-int8",
                   "cached": False, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
        "timings_ms": {"vad": round(t_vad * 1000), "tagging": round(t_tag * 1000), "asr": round(t_asr * 1000)},
    }
    print(json.dumps(digest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
