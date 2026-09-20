"""Silero VAD 帧概率 + 双阈值状态机。

为什么不用 sherpa 的 VoiceActivityDetector：它只有单阈值，M0 素材上起点晚 180ms（"帮"字前 190ms 概率 0.3–0.4）。
双阈值：起点用低阈值（默认 0.20）接住软起音，终点用高阈值（默认 0.50）少拖尾；32 ms 一帧，起点回补一帧。
实测记录见 docs/结果-T2.md 的 2026-09-20 复测。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SR = 16000
WINDOW = 512                      # Silero v4 @16k 的输入窗，32 ms
HOP_SEC = WINDOW / SR


@dataclass
class VadParams:
    on_threshold: float = 0.20
    off_threshold: float = 0.50
    min_speech_sec: float = 0.25
    min_silence_sec: float = 0.25
    onset_pad_sec: float = HOP_SEC   # 起点回补一帧：概率在窗内上升，窗起点晚于真实起音


class SileroVad:
    def __init__(self, model_path: str, num_threads: int = 1):
        import onnxruntime as ort  # 只有引擎进程需要；speech_segments 是纯函数，A 的测试也 import 本模块

        so = ort.SessionOptions()
        so.intra_op_num_threads = num_threads
        so.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(model_path, so, providers=["CPUExecutionProvider"])
        names = {i.name for i in self.sess.get_inputs()}
        self._v5 = "state" in names          # silero v5 用单个 state；v4 用 h / c

    def probabilities(self, x: np.ndarray) -> np.ndarray:
        """每 32 ms 一个语音概率；末尾不足一窗补零。"""
        n = int(np.ceil(len(x) / WINDOW))
        buf = np.zeros(n * WINDOW, dtype=np.float32)
        buf[: len(x)] = x
        out = np.empty(n, dtype=np.float32)
        if self._v5:
            state = np.zeros((2, 1, 128), np.float32)
            sr = np.array(SR, dtype=np.int64)
            for i in range(n):
                p, state = self.sess.run(None, {"input": buf[i * WINDOW:(i + 1) * WINDOW][None, :], "state": state, "sr": sr})
                out[i] = p.reshape(-1)[0]
        else:
            h = np.zeros((2, 1, 64), np.float32)
            c = np.zeros((2, 1, 64), np.float32)
            for i in range(n):
                p, h, c = self.sess.run(None, {"x": buf[i * WINDOW:(i + 1) * WINDOW][None, :], "h": h, "c": c})
                out[i] = p.reshape(-1)[0]
        return out


def speech_segments(probs: np.ndarray, duration_sec: float, p: VadParams) -> list[tuple[float, float]]:
    """概率序列 → [start, end) 语音区间（秒），已做最短语音 / 最短静音 / 起点回补。"""
    segs: list[tuple[float, float]] = []
    in_speech = False
    start = 0
    silence = 0
    for i, v in enumerate(probs):
        if not in_speech:
            if v >= p.on_threshold:
                in_speech, start, silence = True, i, 0
        else:
            if v < p.off_threshold:
                silence += 1
                if silence * HOP_SEC >= p.min_silence_sec:
                    segs.append((start, i - silence + 1))
                    in_speech = False
            else:
                silence = 0
    if in_speech:
        segs.append((start, len(probs)))
    out: list[tuple[float, float]] = []
    for s, e in segs:
        if (e - s) * HOP_SEC < p.min_speech_sec:
            continue
        st = max(0.0, s * HOP_SEC - p.onset_pad_sec, out[-1][1] if out else 0.0)
        en = min(duration_sec, e * HOP_SEC)
        if en > st:
            out.append((round(st, 3), round(en, 3)))
    return out
