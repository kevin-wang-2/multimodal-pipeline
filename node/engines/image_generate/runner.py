from __future__ import annotations

import base64
import gc
import io
import os
import sys
import time
from pathlib import Path

from engines.image_generate import PARAMS_SCHEMA


def request(job: dict) -> tuple[str, dict]:
    arguments = job.get("arguments") or {}
    params = {k: v["default"] for k, v in PARAMS_SCHEMA["properties"].items()}
    params.update(arguments.get("options") or {})
    if params["width"] * params["height"] > 1048576:
        raise ValueError("width * height must not exceed 1048576 pixels")
    prompt = str(arguments.get("prompt") or "").strip()
    if not prompt or len(prompt) > 4000:
        raise ValueError("prompt must contain 1 to 4000 characters")
    return prompt, params


def prepare_backend():
    from engines.common.io import log
    root = Path(os.environ["MMP_COMFY_ROOT"]).resolve()
    sys.path.insert(0, str(root))
    # Comfy 只作为库运行；模型缓存随本引擎进程结束，不启动独立 HTTP 服务。
    sys.argv = [str(root / "main.py"), "--disable-pinned-memory", "--fast-disk",
                "--base-directory", str(root),
                "--reserve-vram", os.environ.get("MMP_KREA_RESERVE_VRAM_GB", "2")]
    import comfy.options
    comfy.options.enable_args_parsing()
    log("initializing Comfy CUDA runtime")
    import torch
    import psutil
    import nodes
    import comfy.model_management as memory
    from PIL import Image
    log("Comfy CUDA runtime ready")
    return torch, psutil, nodes, memory, Image


def generate(prompt: str, params: dict, cancelled) -> tuple[dict, dict]:
    from engines.common.io import log
    torch, psutil, nodes, memory, Image = prepare_backend()

    memory.interrupt_current_processing(False)
    torch.cuda.reset_peak_memory_stats()
    started = time.monotonic()

    def release():
        memory.unload_all_models()
        gc.collect()
        memory.soft_empty_cache(force=True)

    def check_cancel():
        if cancelled.is_set():
            raise RuntimeError("generation cancelled")

    with torch.inference_mode():
        check_cancel()
        # 三阶段顺序加载，避免文本编码器、DiT、VAE 同时占用主机内存。
        log("loading text encoder")
        clip = nodes.CLIPLoader().load_clip(
            os.environ.get("MMP_KREA_TEXT_ENCODER", "qwen3vl_4b_fp8_scaled.safetensors"), "krea2")[0]
        log("encoding prompt")
        positive = nodes.CLIPTextEncode().encode(clip, prompt)[0]
        negative = nodes.ConditioningZeroOut().zero_out(positive)[0]
        del clip
        release()
        check_cancel()

        log("loading diffusion model")
        model = nodes.UNETLoader().load_unet(
            os.environ.get("MMP_KREA_DIFFUSION_MODEL", "krea2_turbo_fp8_scaled.safetensors"), "default")[0]
        latent = nodes.EmptyLatentImage().generate(params["width"], params["height"], 1)[0]
        # Comfy 的 cfg=1 表示不做 classifier-free guidance，匹配官方 Turbo 工作流。
        log("sampling image")
        sampled = nodes.KSampler().sample(model, params["seed"], 8, 1.0, "euler", "simple",
                                           positive, negative, latent)[0]
        del model, positive, negative, latent
        release()
        check_cancel()

        log("decoding image")
        vae = nodes.VAELoader().load_vae(
            os.environ.get("MMP_KREA_VAE", "qwen_image_vae.safetensors"))[0]
        pixels = nodes.VAEDecodeTiled().decode(vae, sampled, 512, 64)[0][0]
        array = (pixels.detach().cpu().numpy().clip(0, 1) * 255).round().astype("uint8")
        buffer = io.BytesIO()
        Image.fromarray(array).save(buffer, format="PNG")
        del vae, sampled, pixels
        release()
        torch.cuda.synchronize()

    result = {"content_type": "image/png", "data_base64": base64.b64encode(buffer.getvalue()).decode(),
              "width": params["width"], "height": params["height"], "seed": params["seed"]}
    metrics = {"vram_peak_mb": round(torch.cuda.max_memory_allocated() / 1048576, 1),
               "generate_ms": int((time.monotonic() - started) * 1000)}
    host_memory = psutil.Process().memory_info()
    if hasattr(host_memory, "peak_wset"):
        metrics["ram_peak_mb"] = round(host_memory.peak_wset / 1048576, 1)
        metrics["commit_peak_mb"] = round(host_memory.peak_pagefile / 1048576, 1)
    return result, metrics
