#!/usr/bin/env python3
"""T2: 用 sherpa-onnx 跑「Silero VAD → SenseVoiceSmall」两级预检，CPU。

用法:
  .venv/bin/python tools/t2_sensevoice.py [wav] [--no-vad] [--model int8|fp32]
"""
import sys, time, wave
import numpy as np
import sherpa_onnx

MODEL_DIR = "models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
VAD_PATH = "models/silero_vad.onnx"
SR = 16000
WINDOW = 512  # 32ms @16k

args = [a for a in sys.argv[1:] if not a.startswith("--")]
flags = [a for a in sys.argv[1:] if a.startswith("--")]
WAV = args[0] if args else "testdata/m0_hum_then_speech.wav"
USE_VAD = "--no-vad" not in flags
QUANT = "int8" if "--model=fp32" not in flags else "fp32"


def read_wav(p):
    with wave.open(p) as w:
        assert w.getframerate() == SR and w.getnchannels() == 1, "需要 16kHz 单声道"
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def main():
    samples = read_wav(WAV)
    dur = len(samples) / SR
    print(f"素材 {WAV}  {dur:.2f}s  VAD={USE_VAD}  model={QUANT}")

    t_build = time.time()
    rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=f"{MODEL_DIR}/model.{QUANT}.onnx",
        tokens=f"{MODEL_DIR}/tokens.txt",
        num_threads=4, use_itn=True, language="zh", debug=False)
    t_recognizer = time.time() - t_build

    segments = []
    t_vad = 0.0
    if USE_VAD:
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = VAD_PATH
        cfg.silero_vad.min_silence_duration = 0.25
        cfg.silero_vad.min_speech_duration = 0.25
        cfg.sample_rate = SR
        vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)
        t0 = time.time()
        for i in range(0, len(samples), WINDOW):
            vad.accept_waveform(samples[i:i + WINDOW])
        vad.flush()
        while not vad.empty():
            seg = vad.front
            segments.append((seg.start / SR, seg.samples))
            vad.pop()
        t_vad = time.time() - t0
    else:
        segments = [(0.0, samples)]

    total_asr = 0.0
    print(f"\n--- VAD 切出 {len(segments)} 段（耗时 {t_vad*1000:.1f} ms）---")
    for start, seg in segments:
        st = rec.create_stream()
        t0 = time.time()
        st.accept_waveform(SR, seg)
        rec.decode_stream(st)
        dt = time.time() - t0
        total_asr += dt
        r = st.result
        extra = []
        for f in ("lang", "emotion", "event"):
            v = getattr(r, f, None)
            if v:
                extra.append(f"{f}={v}")
        print(f"[{start:6.2f}s → {start+len(seg)/SR:6.2f}s] ({dt*1000:7.1f} ms)  {r.text}   {' '.join(extra)}")

    print(f"\n加载 recognizer {t_recognizer*1000:.0f} ms | VAD {t_vad*1000:.0f} ms | ASR 合计 {total_asr*1000:.0f} ms")
    tot = t_vad + total_asr
    print(f"端到端（不含加载）{tot*1000:.0f} ms  →  对 {dur:.1f}s 素材 = {dur/tot if tot else 0:.1f}x 实时")


if __name__ == "__main__":
    main()
