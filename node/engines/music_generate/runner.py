from __future__ import annotations

import base64
import io
import os
import time
from functools import cache, partial
from importlib.metadata import version
from pathlib import Path

from engines.music_generate import EXPECTED_WEIGHTS, OPTIONS_SCHEMA, RUNTIME_VERSION


def request(job: dict) -> dict:
    arguments = job.get("arguments") or {}
    result = {}
    for name, maximum in (("style", 4000), ("lyrics", 12000), ("score", 50000)):
        value = arguments.get(name)
        if name == "score" and value is None:
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"{name} must contain 1 to {maximum} characters")
        result["abc" if name == "score" else name] = value
    options = arguments.get("options") or {}
    seed = options.get("seed", OPTIONS_SCHEMA["properties"]["seed"]["default"])
    if type(seed) is not int or not 0 <= seed < 2**63:
        raise ValueError("seed must be an integer in [0, 2**63)")
    return {**result, "seed": seed, "cot": "full"}


@cache
def prepare_backend():
    import torch
    import soundfile
    import yue2.cuda_graph as cuda_graph
    from yue2 import YuE2Pipeline
    from torch.nn.attention import SDPBackend, sdpa_kernel

    if version("yue2-infer") != RUNTIME_VERSION or torch.__version__ != "2.10.0+cu128":
        raise RuntimeError("YuE2 requires yue2-infer 0.1.6 and torch 2.10.0+cu128")
    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported() or not torch.backends.cudnn.is_available():
        raise RuntimeError("YuE2 requires a BF16 NVIDIA GPU and cuDNN")
    # The pinned runtime checks only whether the FlashAttention op exists;
    # Windows exposes a stub. Select its existing cuDNN graph implementation.
    cuda_graph.GraphAR = partial(cuda_graph.GraphAR, attention_backend="cudnn")
    return torch, soundfile, YuE2Pipeline, SDPBackend, sdpa_kernel


def generate(values: dict, cancelled) -> tuple[dict, dict, dict]:
    torch, soundfile, Pipeline, Backend, sdpa_kernel = prepare_backend()
    root = Path(os.environ["MMP_YUE2_ROOT"]).resolve()
    torch.cuda.reset_peak_memory_stats()
    started = time.monotonic()
    with Pipeline.from_pretrained(str(root / "model"), vae=str(root / "vae"),
                                 device="cuda", local_files_only=True,
                                 backend="torch", quantization="none") as pipe:
        if pipe.weights != EXPECTED_WEIGHTS:
            raise RuntimeError("YuE2 local weights/config do not match the pinned revisions")
        if cancelled.is_set():
            raise InterruptedError("generation cancelled")
        # Force cuDNN for AR prefill and NAR as well as graph decode. Failure is
        # explicit: never silently fall back to the large, slow math kernel.
        with sdpa_kernel(Backend.CUDNN_ATTENTION):
            song = pipe(**values, cancelled=cancelled.is_set)
        if cancelled.is_set():
            raise InterruptedError("generation cancelled")
        torch.cuda.synchronize()
        buffer = io.BytesIO()
        soundfile.write(buffer, song.audio, song.sample_rate, format="FLAC", subtype="PCM_24")
        timings = {"abc": int(song.timing["abc"]["seconds"] * 1000),
                   "semantic": int(song.timing["semantic"]["seconds"] * 1000),
                   "nar": int(song.timing["nar_seconds"] * 1000),
                   "vae": int(song.timing["vae_seconds"] * 1000),
                   "generate": int((time.monotonic() - started) * 1000)}
        outputs = {
            "audio": {"kind": "media", "content_type": "audio/flac",
                      "data_base64": base64.b64encode(buffer.getvalue()).decode("ascii")},
            "score": {"kind": "text", "text": song.abc},
            "metadata": {"kind": "json", "value": {
                "sample_rate": song.sample_rate, "channels": song.audio.shape[1],
                "duration_sec": len(song.audio) / song.sample_rate, "seed": values["seed"],
                "truncated": song.truncated, "attention": "cudnn", "ar_execution": "cuda_graph",
                "model_dtype": "bfloat16", "vae_dtype": "float32", "ode_steps": 32,
                "timings_ms": timings,
            }},
        }
        metrics = {"vram_peak_mb": round(torch.cuda.max_memory_allocated() / 1048576, 1),
                   "generate_ms": timings["generate"]}
        return outputs, timings, metrics
