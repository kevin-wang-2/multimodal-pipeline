"""读 WAV → 16 kHz 单声道 float32。M1 只接 PCM WAV（8/16/24/32 bit），别的容器交给 gaps 说明。"""
from __future__ import annotations

import wave

import numpy as np

SR = 16000


class UnsupportedAudio(Exception):
    pass


def read_wav_16k(path: str) -> np.ndarray:
    try:
        with wave.open(path) as w:
            nch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
    except (wave.Error, EOFError) as e:
        raise UnsupportedAudio(f"not a PCM WAV file: {e}") from e
    if sw == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128
    elif sw == 2:
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
    elif sw == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        x = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int8).astype(np.int32) << 16)).astype(np.float32) / 8388608
    elif sw == 4:
        x = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648
    else:
        raise UnsupportedAudio(f"unsupported sample width {sw}")
    if nch > 1:
        x = x.reshape(-1, nch).mean(axis=1)
    if sr != SR:
        x = resample_linear(x, sr, SR)
    return np.ascontiguousarray(x, dtype=np.float32)


def resample_linear(x: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    """线性插值重采样。预检只要边界与粗类别，够用；能力任务需要高保真时自己重采。"""
    if len(x) == 0:
        return x
    n_to = int(round(len(x) * sr_to / sr_from))
    t_from = np.arange(len(x)) / sr_from
    t_to = np.arange(n_to) / sr_to
    return np.interp(t_to, t_from, x).astype(np.float32)
