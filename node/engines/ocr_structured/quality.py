"""每页质量信号与升级触发（T5 §4.2 第一版：低置信行占比、覆盖率异常）。纯函数，单测直接打。"""
from __future__ import annotations

import numpy as np

INK_THRESHOLD = 128   # 灰度低于此算墨迹


def ink_mask(gray: np.ndarray) -> np.ndarray:
    return gray < INK_THRESHOLD


def boxes_mask(shape: tuple[int, int], boxes: list[list[list[float]]]) -> np.ndarray:
    """多边形检测框 → 覆盖掩码。用轴对齐外接矩形近似（覆盖率是粗信号，不值得光栅化多边形）。"""
    m = np.zeros(shape, dtype=bool)
    h, w = shape
    for poly in boxes:
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        x0, x1 = max(0, int(min(xs))), min(w, int(np.ceil(max(xs))))
        y0, y1 = max(0, int(min(ys))), min(h, int(np.ceil(max(ys))))
        if x1 > x0 and y1 > y0:
            m[y0:y1, x0:x1] = True
    return m


def rule_mask(ink: np.ndarray, min_len_frac: float = 0.06) -> np.ndarray:
    """表格框线 / 分隔线：连续墨迹长度 ≥ 页宽（高）× min_len_frac 的水平 / 垂直段。用 cumsum 做滑窗，不依赖 scipy。"""
    out = np.zeros_like(ink)
    for axis in (1, 0):
        a = ink if axis == 1 else ink.T
        L = max(20, int(a.shape[1] * min_len_frac))
        if a.shape[1] <= L:
            continue
        cs = np.zeros((a.shape[0], a.shape[1] + 1), dtype=np.int32)
        cs[:, 1:] = np.cumsum(a, axis=1)
        full = (cs[:, L:] - cs[:, :-L]) == L          # 起点 i 的窗 [i, i+L) 全是墨迹
        # 把每个满窗的 L 个像素都标上：对 full 再做一次长度 L 的"任意一个为真"滑窗
        fc = np.zeros((a.shape[0], full.shape[1] + 1), dtype=np.int32)
        fc[:, 1:] = np.cumsum(full, axis=1)
        marked = np.zeros_like(a)
        for i in range(a.shape[1]):
            lo, hi = max(0, i - L + 1), min(full.shape[1], i + 1)
            if hi > lo:
                marked[:, i] = (fc[:, hi] - fc[:, lo]) > 0
        out |= marked if axis == 1 else marked.T
    return out


def page_quality(gray: np.ndarray, texts: list[str], scores: list[float], boxes: list, low_conf_threshold: float) -> dict:
    ink = ink_mask(gray)
    ink = ink & ~rule_mask(ink)      # 框线不是文字：不算进"该被认出来的墨迹"
    ink_total = int(ink.sum())
    covered = int((ink & boxes_mask(gray.shape, boxes)).sum()) if boxes else 0
    n = len(texts)
    q = {
        "lines": n,
        "chars": sum(len(t) for t in texts),
        "mean_score": round(float(np.mean(scores)), 4) if scores else 0.0,
        "low_conf_ratio": round(sum(1 for s in scores if s < low_conf_threshold) / n, 4) if n else 0.0,
        "ink_ratio": round(ink_total / ink.size, 5) if ink.size else 0.0,   # 去框线后的文字墨迹占比
        "coverage": round(covered / ink_total, 4) if ink_total else 1.0,
    }
    return q


def flags_for(q: dict, low_conf_ratio_max: float, coverage_min: float, min_ink_ratio: float = 0.002) -> list[str]:
    """空白页不报覆盖率异常（没墨迹就没什么可漏）。"""
    out: list[str] = []
    if q["lines"] == 0:
        if q.get("ink_ratio", 0) >= min_ink_ratio:
            out.append("empty")          # 有墨迹却一行都没认出来：最严重的静默丢内容
        return out
    if q["low_conf_ratio"] > low_conf_ratio_max:
        out.append("low_confidence")
    if q.get("ink_ratio", 0) >= min_ink_ratio and q["coverage"] < coverage_min:
        out.append("coverage_anomaly")
    return out
