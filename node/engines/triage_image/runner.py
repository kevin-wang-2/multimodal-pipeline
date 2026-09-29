from __future__ import annotations

import os
import time
from pathlib import Path

from PIL import Image, ImageOps

from engines.common.io import BadParams, serve
from engines.ocr_structured.pages import UnsupportedDocument, load_pages, sniff
from engines.triage_image import PARAMS_SCHEMA
from engines.triage_image.digest import RawRegion, build_digest


MIME = {"png": "image/png", "jpeg": "image/jpeg", "tiff": "image/tiff", "webp": "image/webp", "pdf": "application/pdf"}


class Florence:
    def __init__(self, model_path: str):
        import torch
        from transformers import AutoProcessor, Florence2ForConditionalGeneration
        self.torch = torch
        self.processor = AutoProcessor.from_pretrained(model_path, local_files_only=True)
        self.model = Florence2ForConditionalGeneration.from_pretrained(
            model_path, local_files_only=True, dtype=torch.float16).eval().to("cuda")

    def task(self, image, task: str, text: str | None = None):
        prompt = task if text is None else task + text
        inputs = self.processor(text=prompt, images=image, return_tensors="pt")
        inputs = {k: v.to("cuda", dtype=self.torch.float16) if v.is_floating_point() else v.to("cuda")
                  for k, v in inputs.items()}
        with self.torch.inference_mode():
            ids = self.model.generate(**inputs, max_new_tokens=768, num_beams=3, do_sample=False)
        generated = self.processor.batch_decode(ids, skip_special_tokens=False)[0]
        return self.processor.post_process_generation(generated, task=task, image_size=image.size).get(task)


def _params(raw: dict) -> dict:
    allowed = set(PARAMS_SCHEMA["properties"])
    if extra := set(raw) - allowed:
        raise BadParams(f"unknown params {sorted(extra)}")
    out = {k: v["default"] for k, v in PARAMS_SCHEMA["properties"].items() if "default" in v}
    out.update(raw)
    return out


def _surfaces(path: str, dpi: int):
    with open(path, "rb") as f:
        kind = sniff(f.read(16))
    if kind == "pdf":
        _, pages = load_pages(path, None, dpi)
        if not pages:
            raise UnsupportedDocument("PDF has no pages")
        return kind, [(p.image, {"id": f"page_{p.index - 1}", "kind": "page", "index": p.index - 1,
                                  "width_px": p.image.width, "height_px": p.image.height, "rotation_deg": 0}) for p in pages]
    source = Image.open(path)
    count = getattr(source, "n_frames", 1)
    items = []
    for index in range(count):
        if count > 1:
            source.seek(index)
        image = ImageOps.exif_transpose(source.copy()).convert("RGB")
        surface_kind = "page" if count > 1 else "image"
        surface_id = f"page_{index}" if count > 1 else "image_0"
        items.append((image, {"id": surface_id, "kind": surface_kind, "index": index,
                              "width_px": image.width, "height_px": image.height, "rotation_deg": 0}))
    return kind, items


def analyze(model: Florence, job: dict) -> tuple[dict, str | None]:
    p = _params(job.get("params") or {})
    try:
        kind, items = _surfaces(job["media_path"], p["pdf_dpi"])
    except UnsupportedDocument as e:
        raise BadParams(f"unsupported media: {e}") from e
    captions, regions, timings = [], [], {"metadata": 0}
    malformed = 0
    for image, surface in items:
        t0 = time.monotonic(); caption = str(model.task(image, "<MORE_DETAILED_CAPTION>") or "").strip()
        timings["caption"] = timings.get("caption", 0) + int((time.monotonic() - t0) * 1000)
        captions.append(caption)
        t0 = time.monotonic(); grounding = model.task(image, "<CAPTION_TO_PHRASE_GROUNDING>", caption) or {}
        timings["grounding"] = timings.get("grounding", 0) + int((time.monotonic() - t0) * 1000)
        t0 = time.monotonic(); detection = model.task(image, "<OD>") or {}
        timings["detection"] = timings.get("detection", 0) + int((time.monotonic() - t0) * 1000)
        for output, tool in ((grounding, "phrase_grounding"), (detection, "object_detection")):
            boxes, labels = output.get("bboxes", []), output.get("labels", [])
            malformed += abs(len(boxes) - len(labels))
            regions.extend(RawRegion(surface["id"], str(label), box, tool) for box, label in zip(boxes, labels))
    return build_digest(job["media_id"], MIME[kind], [s for _, s in items], captions, regions,
                        malformed, p["region_dedupe_iou"], timings)


def main(engine: str, version: str, default_model: str, allow_retry: bool) -> None:
    model_path = os.environ.get("MMP_FLORENCE_MODEL", default_model)
    if not Path(model_path).is_dir():
        raise SystemExit(f"model missing: {model_path}")
    model = Florence(model_path)

    async def run(job, cancelled):
        import asyncio
        model.torch.cuda.reset_peak_memory_stats()
        digest, reason = await asyncio.to_thread(analyze, model, job)
        model.torch.cuda.synchronize()
        metrics = {"vram_peak_mb": round(model.torch.cuda.max_memory_allocated() / 1048576, 1)}
        if reason and allow_retry:
            return None, digest.get("timings_ms", {}), {"engine": "triage_image_large", "reason": reason}, metrics
        return digest, digest.get("timings_ms", {}), None, metrics

    serve(engine, version, run)
