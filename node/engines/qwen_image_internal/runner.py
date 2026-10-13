from __future__ import annotations

import base64
import gc
import io
import os
import sys
import time
from functools import cache
from pathlib import Path

from engines.qwen_image_internal import (
    DIFFUSION_MODEL,
    EDIT_OPTIONS_SCHEMA,
    GENERATE_OPTIONS_SCHEMA,
    TEXT_ENCODER,
    VAE_MODEL,
)


def _options(schema: dict, supplied: dict | None) -> dict:
    values = {name: spec["default"] for name, spec in schema["properties"].items()}
    values.update(supplied or {})
    return values


def request(job: dict) -> tuple[str, str, Path | None, dict]:
    task_type = str(job.get("task_type") or "")
    arguments = job.get("arguments") or {}
    prompt = str(arguments.get("prompt") or "").strip()
    if not prompt or len(prompt) > 4000:
        raise ValueError("prompt must contain 1 to 4000 characters")
    if task_type == "image.generate.qwen":
        params = _options(GENERATE_OPTIONS_SCHEMA, arguments.get("options"))
        if params["width"] * params["height"] > 1048576:
            raise ValueError("width * height must not exceed 1048576 pixels")
        return "generate", prompt, None, params
    if task_type == "image.edit.qwen":
        image = arguments.get("image") or {}
        path = Path(str(image.get("path") or ""))
        if not path.is_file():
            raise ValueError("image must resolve to a readable local file")
        return "edit", prompt, path, _options(EDIT_OPTIONS_SCHEMA, arguments.get("options"))
    raise ValueError(f"unsupported task type {task_type!r}")


@cache
def prepare_backend():
    from engines.common.io import log

    root = Path(os.environ["MMP_QWEN_COMFY_ROOT"]).resolve()
    sys.path.insert(0, str(root))
    sys.argv = [
        str(root / "main.py"),
        "--disable-pinned-memory",
        "--fast-disk",
        "--base-directory",
        str(root),
        "--reserve-vram",
        os.environ.get("MMP_QWEN_RESERVE_VRAM_GB", "1"),
    ]
    import comfy.options

    comfy.options.enable_args_parsing()
    log("initializing Qwen Image 2.1 CUDA runtime")
    import numpy as np
    import psutil
    import torch
    from PIL import Image
    import comfy.model_management as memory
    import nodes
    from comfy_extras.nodes_qwen import QwenImage21Cache, TextEncodeQwenImage21

    log("Qwen Image 2.1 CUDA runtime ready")
    return torch, np, psutil, Image, nodes, memory, QwenImage21Cache, TextEncodeQwenImage21


def render(mode: str, prompt: str, input_path: Path | None, params: dict, cancelled) -> tuple[dict, dict]:
    from engines.common.io import log

    torch, np, psutil, Image, nodes, memory, QwenImage21Cache, TextEncodeQwenImage21 = prepare_backend()
    memory.interrupt_current_processing(False)
    torch.cuda.reset_peak_memory_stats()
    started = time.monotonic()
    stages: dict[str, int] = {}

    def timed(name: str, function):
        stage_started = time.monotonic()
        value = function()
        stages[name] = int((time.monotonic() - stage_started) * 1000)
        log(f"{name}: {stages[name]} ms")
        return value

    def release() -> None:
        memory.unload_all_models()
        gc.collect()
        memory.soft_empty_cache(force=True)

    def check_cancel() -> None:
        if cancelled.is_set():
            raise RuntimeError("generation cancelled")

    def load_input(path: Path):
        image = Image.open(path).convert("RGBA")
        array = np.asarray(image).astype(np.float32) / 255.0
        return torch.from_numpy(array)[None,]

    output_image = None
    try:
        with torch.inference_mode():
            check_cancel()
            vae = None
            images = None
            if mode == "edit":
                vae = timed("load_vae", lambda: nodes.VAELoader().load_vae(VAE_MODEL)[0])
                images = {"image_1": timed("load_input", lambda: load_input(input_path))}

            clip = timed(
                "load_text_encoder",
                lambda: nodes.CLIPLoader().load_clip(TEXT_ENCODER, "qwen_image")[0],
            )
            resolution = params.get("resolution", 1024)
            positive, negative, latent = timed(
                "encode",
                lambda: TextEncodeQwenImage21.execute(
                    clip=clip,
                    prompt=prompt,
                    negative_prompt="",
                    vae=vae,
                    resolution=resolution,
                    images=images,
                ).result,
            )
            if mode == "generate":
                latent = {"samples": torch.zeros(
                    [1, 64, params["height"] // 16, params["width"] // 16],
                    device=memory.intermediate_device(),
                )}
            del clip, images
            if vae is not None:
                del vae
            release()
            check_cancel()

            model = timed(
                "load_diffusion",
                lambda: nodes.UNETLoader().load_unet(DIFFUSION_MODEL, "default")[0],
            )
            model = QwenImage21Cache.execute(model=model, device="auto", dtype="int8").result[0]
            sampled = timed(
                "sample",
                lambda: nodes.KSampler().sample(
                    model, params["seed"], 8, 1.0, "euler", "simple",
                    positive, negative, latent,
                )[0],
            )
            del model, positive, negative, latent
            release()
            check_cancel()

            vae = timed("reload_vae", lambda: nodes.VAELoader().load_vae(VAE_MODEL)[0])
            pixels = timed("decode", lambda: nodes.VAEDecodeTiled().decode(vae, sampled, 512, 64)[0][0])
            array = (pixels.detach().cpu().numpy().clip(0, 1) * 255).round().astype("uint8")
            output_image = Image.fromarray(array, mode="RGBA" if array.shape[-1] == 4 else "RGB")
            buffer = io.BytesIO()
            output_image.save(buffer, format="PNG")
            del vae, sampled, pixels
            release()
            torch.cuda.synchronize()
    finally:
        release()

    if output_image is None:
        raise RuntimeError("Qwen produced no image")
    host = psutil.Process().memory_info()
    result = {
        "content_type": "image/png",
        "data_base64": base64.b64encode(buffer.getvalue()).decode(),
        "mode": mode,
        "width": output_image.width,
        "height": output_image.height,
        "seed": params["seed"],
        "timings_ms": stages,
    }
    metrics = {
        "vram_peak_mb": round(torch.cuda.max_memory_allocated() / 1048576, 1),
        "render_ms": int((time.monotonic() - started) * 1000),
    }
    if hasattr(host, "peak_wset"):
        metrics["ram_peak_mb"] = round(host.peak_wset / 1048576, 1)
        metrics["commit_peak_mb"] = round(host.peak_pagefile / 1048576, 1)
    return result, metrics
