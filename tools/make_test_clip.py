#!/usr/bin/env python3
"""生成 M0 测试素材：哼唱段(2s) + 口述指令段(2s)，单声道 16kHz WAV。

用法: python3 tools/make_test_clip.py [输出路径]
仅用标准库（wave/math/array）；语音段在 macOS 上借 `say` + `afconvert`，
其他平台会跳过语音段并提示。
"""
import math, os, subprocess, sys, tempfile, wave
from array import array

SR = 16000
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "testdata", "m0_hum_then_speech.wav")


def hum(dur=2.0, sr=SR):
    """合成一段带颤音的哼唱：C 大调琶音 C5-E5-G5-E5，加轻微滑音。"""
    notes = [523.25, 659.25, 783.99, 659.25]
    seg = dur / len(notes)
    out = array("h")
    n_total = int(dur * sr)
    for i in range(n_total):
        t = i / sr
        k = min(int(t / seg), len(notes) - 1)
        f = notes[k] * (1.0 + 0.02 * math.sin(2 * math.pi * 5.0 * t))  # 颤音
        env = min(1.0, t / 0.05) * min(1.0, (dur - t) / 0.10)          # 淡入淡出
        env *= 0.7
        # 加一点二次谐波，更像人声而不是纯正弦
        s = math.sin(2 * math.pi * f * t) + 0.25 * math.sin(2 * math.pi * 2 * f * t)
        out.append(int(max(-1.0, min(1.0, s * env * 0.5)) * 32767))
    return out


def speech(text, sr=SR):
    """macOS: 用 say 生成 aiff，再用 afconvert 转 16k 单声道 wav。"""
    if sys.platform != "darwin":
        print("[warn] 非 macOS：跳过语音段（请自行补一段中文语音）", file=sys.stderr)
        return array("h", [0] * int(2.0 * sr))
    with tempfile.TemporaryDirectory() as d:
        aiff, wav = os.path.join(d, "s.aiff"), os.path.join(d, "s.wav")
        subprocess.run(["say", "-v", "Tingting", "-o", aiff, text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{sr}", "-c", "1", aiff, wav], check=True)
        with wave.open(wav) as w:
            data = array("h")
            data.frombytes(w.readframes(w.getnframes()))
        return data


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    pcm = hum(2.0) + speech("帮我把这段的谱子扒出来")
    with wave.open(OUT, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    print(f"wrote {OUT}  ({len(pcm)/SR:.2f}s, 16kHz mono)")


if __name__ == "__main__":
    main()
