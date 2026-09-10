#!/usr/bin/env python3
"""T3: AED 时序路由验证 —— 用 sherpa-onnx 的音频打标模型滑窗，看能否切出「哼唱段/语音段」边界。

模型：sherpa-onnx-zipformer-small-audio-tagging-2024-04-15（AudioSet 类，clip 级）
做法：虽然模型是 clip 级（无时序定位），我们用**滑窗**得到伪帧级置信度曲线 —— 正是路由要的形状。

用法: .venv/bin/python tools/t3_aed_routing.py [wav] [--win 1.0] [--hop 0.1] [--topk 5]
"""
import argparse, os, sys, wave, time
import numpy as np
import sherpa_onnx

MODEL_DIR = "models/sherpa-onnx-zipformer-small-audio-tagging-2024-04-15"
SR = 16000
# 我们关心的类（AudioSet 里的大类/子类）
INTEREST = {
    "Speech": ["Speech", "Male speech", "Female speech", "Conversation", "Narration"],
    "Music": ["Music", "Musical instrument", "Singing", "Humming", "Vocal music", "Synthesizer", "Keyboard (musical)", "Piano"],
}


def read_wav(p):
    with wave.open(p) as w:
        assert w.getframerate() == SR and w.getnchannels() == 1, "需要 16kHz 单声道"
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def load(path):
    cfg = sherpa_onnx.AudioTaggingConfig(
        model=sherpa_onnx.AudioTaggingModelConfig(
            zipformer=sherpa_onnx.OfflineZipformerAudioTaggingModelConfig(model=path),
            num_threads=4, debug=False),
        labels=os.path.join(MODEL_DIR, "class_labels_indices.csv"),
        top_k=10)
    if not cfg.validate():
        sys.exit("模型配置无效")
    return sherpa_onnx.AudioTagging(cfg)


def tag(tagger, samples, cache):
    st = tagger.create_stream()
    st.accept_waveform(sample_rate=SR, waveform=samples)
    res = tagger.compute(st)  # -> list[AudioEvent(index,name,prob)]
    if "probe" not in cache:
        cache["probe"] = True
        if res:
            print("[probe] AudioEvent attrs:",
                  [a for a in dir(res[0]) if not a.startswith("_")], file=sys.stderr)
    return [(e.name, float(e.prob)) for e in res]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", nargs="?", default="testdata/m0_hum_then_speech.wav")
    ap.add_argument("--win", type=float, default=1.0)
    ap.add_argument("--hop", type=float, default=0.1)
    ap.add_argument("--topk", type=int, default=4)
    a = ap.parse_args()

    samples = read_wav(a.wav)
    dur = len(samples) / SR
    print(f"素材 {a.wav}  {dur:.2f}s   滑窗 {a.win}s / 步进 {a.hop}s")

    tagger = load(os.path.join(MODEL_DIR, "model.onnx"))
    W = int(a.win * SR); H = int(a.hop * SR)
    cache = {}
    t0 = time.time()
    rows = []
    for s in range(0, max(1, len(samples) - W + 1), H):
        seg = samples[s:s + W]
        if len(seg) < W:
            seg = np.pad(seg, (0, W - len(seg)))
        tops = tag(tagger, seg, cache)[:a.topk]
        rows.append((s / SR, tops))
    dt = time.time() - t0

    print(f"\n滑窗数 {len(rows)}，耗时 {dt:.1f}s（{dt/len(rows)*1000:.0f} ms/窗）\n")
    print(f"{'起点':>6}  顶部标签")
    for t, tops in rows:
        print(f"{t:6.2f}  " + "  ".join(f"{l}:{p:.2f}" for l, p in tops))

    # 用关心的类聚合出两条曲线，找交叉点
    def curve(kind):
        out = []
        for t, tops in rows:
            v = 0.0
            for l, p in tops:
                if l in INTEREST[kind]:
                    v = max(v, p)
            out.append(v)
        return np.array(out)
    cs, cm = curve("Speech"), curve("Music")
    if len(cs) > 1:
        cross = None
        for i in range(1, len(cs)):
            if (cm[i - 1] >= cs[i - 1]) and (cm[i] < cs[i]):
                cross = rows[i][0]
                break
        print(f"\nSpeech 曲线均值 {cs.mean():.3f} | Music 曲线均值 {cm.mean():.3f}")
        print(f"Music→Speech 交叉点 ≈ {cross}s" if cross else "未检测到 Music→Speech 交叉")


if __name__ == "__main__":
    main()
