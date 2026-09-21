"""ocr.structured 引擎子进程。启动加载 PP-OCR（默认语种），可选预加载 VL（MMP_OCR_PRELOAD_VL=1，默认 1：冷加载 76s 是启动成本不该算进任务）。

paddle 的 predict 不是线程安全的：每个模型一把锁，GPU 上串行；并发只用于媒体解码 / 栅格化。
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

import numpy as np

from engines.common.io import BadParams, log, serve
from engines.ocr_structured import ENGINE_FAST, PARAMS_SCHEMA, VERSION_FAST
from engines.ocr_structured.pages import UnsupportedDocument, load_pages
from engines.ocr_structured.quality import flags_for, page_quality

PRELOAD_VL = os.environ.get("MMP_OCR_PRELOAD_VL", "1") == "1"
os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "modelscope")   # T5 §6 坑 4：国内走 ModelScope 才拉得动模型


class Models:
    def __init__(self):
        from paddleocr import PaddleOCR

        self._PaddleOCR = PaddleOCR
        self.ocr: dict[str, object] = {}
        self.ocr_lock = threading.Lock()
        self.vl = None
        self.vl_lock = threading.Lock()
        t0 = time.monotonic()
        self.fast("ch")
        log(f"pp-ocr loaded in {time.monotonic() - t0:.1f}s")
        if PRELOAD_VL:
            t0 = time.monotonic()
            self.vl_pipeline()
            log(f"paddleocr-vl loaded in {time.monotonic() - t0:.1f}s")

    def fast(self, lang: str):
        if lang not in self.ocr:
            self.ocr[lang] = self._PaddleOCR(lang=lang, use_doc_orientation_classify=False, use_doc_unwarping=False,
                                             use_textline_orientation=False)
        return self.ocr[lang]

    def vl_pipeline(self):
        if self.vl is None:
            from paddleocr import PaddleOCRVL

            self.vl = PaddleOCRVL(use_seal_recognition=True, use_chart_recognition=True, merge_layout_blocks=True)
        return self.vl


M: Models | None = None


def _params(params: dict) -> dict:
    allowed = set(PARAMS_SCHEMA["properties"])
    extra = set(params) - allowed
    if extra:
        raise BadParams(f"unknown params {sorted(extra)}")
    out = {k: v.get("default") for k, v in PARAMS_SCHEMA["properties"].items() if "default" in v}
    out.update(params)
    return out


def _fast_page(m: Models, img, p: dict) -> tuple[dict, list]:
    arr = np.asarray(img)[:, :, ::-1]           # PIL RGB → paddle 要 BGR
    with m.ocr_lock:
        res = m.fast(p["lang"]).predict(arr)
    texts: list[str] = []
    scores: list[float] = []
    polys: list = []
    for r in res:
        d = r if isinstance(r, dict) else getattr(r, "json", {}).get("res", {})
        texts += [str(t) for t in d.get("rec_texts", [])]
        scores += [float(s) for s in d.get("rec_scores", [])]
        polys += [np.asarray(x).tolist() for x in (d.get("rec_polys") or d.get("dt_polys") or [])]
    gray = np.asarray(img.convert("L"))
    q = page_quality(gray, texts, scores, polys, p["low_conf_threshold"])
    lines = [{"text": t, "score": round(s, 4), "box": [[float(x), float(y)] for x, y in poly]}
             for t, s, poly in zip(texts, scores, polys)]
    return q, lines


def _vl_page(m: Models, img) -> tuple[str, list[dict]]:
    arr = np.asarray(img)[:, :, ::-1]
    with m.vl_lock:
        res = m.vl_pipeline().predict(arr)
    md_parts: list[str] = []
    elements: list[dict] = []
    for r in res:
        md = getattr(r, "markdown", None)
        if isinstance(md, dict):
            md_parts.append(str(md.get("markdown_texts") or ""))
        elif md:
            md_parts.append(str(md))
        j = getattr(r, "json", None)
        d = j.get("res", j) if isinstance(j, dict) else {}
        for blk in d.get("parsing_res_list") or []:
            e = {"type": str(blk.get("block_label", "text")), "text": str(blk.get("block_content", ""))}
            bbox = blk.get("block_bbox")
            if bbox is not None and len(bbox) == 4:
                e["box"] = [float(v) for v in bbox]
            elements.append(e)
    return "\n\n".join(md_parts), elements


def run_sync(job: dict) -> dict:
    assert M is not None
    p = _params(job.get("params") or {})
    tier = job["tier"]
    timings: dict[str, int] = {}
    t0 = time.monotonic()
    try:
        page_count, pages = load_pages(job["media_path"], p.get("pages"), p["dpi"])
    except UnsupportedDocument as e:
        raise BadParams(f"unsupported media: {e}") from e
    timings["render"] = int((time.monotonic() - t0) * 1000)
    out_pages = []
    suggest: list[int] = []
    t0 = time.monotonic()
    for pg in pages:
        w, h = pg.image.size
        if tier == "gpu-fast":
            q, lines = _fast_page(M, pg.image, p)
            flags = flags_for(q, p["low_conf_ratio_max"], p["coverage_min"])
            if flags:
                suggest.append(pg.index)
            out_pages.append({"page": pg.index, "tier": tier, "width": w, "height": h,
                              "text": "\n".join(l["text"] for l in lines), "lines": lines, "quality": q, "flags": flags})
        elif tier == "gpu":
            md, elements = _vl_page(M, pg.image)
            texts = [e["text"] for e in elements] or [md]
            q = {"lines": len(elements), "chars": sum(len(t) for t in texts)}
            out_pages.append({"page": pg.index, "tier": tier, "width": w, "height": h, "text": md, "markdown": md,
                              "elements": elements, "quality": q, "flags": ["empty"] if not md.strip() else []})
        else:
            raise BadParams(f"unknown tier {tier}")
    timings["ocr"] = int((time.monotonic() - t0) * 1000)
    return {"page_count": page_count, "pages": out_pages, "suggest_upgrade_pages": suggest, "timings_ms": timings}


async def run(job: dict, cancelled: asyncio.Event) -> tuple[object, dict]:
    result = await asyncio.to_thread(run_sync, job)
    return result, dict(result["timings_ms"])


def main() -> None:
    global M
    try:
        M = Models()
    except Exception as e:  # 模型没下载 / paddle 装坏：以非零退出，A 报 engine_failed 并带 stderr
        print(f"model load failed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(3)
    serve(ENGINE_FAST, VERSION_FAST, run)   # hello 报快档；VL 版本由 tier 元数据声明


if __name__ == "__main__":
    main()
