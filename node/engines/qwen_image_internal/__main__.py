import asyncio
import time

from engines.common.io import BadArguments, serve
from engines.qwen_image_internal import ENGINE, ENGINE_VERSION
from engines.qwen_image_internal.runner import prepare_backend, render, request


async def run(job: dict, cancelled: asyncio.Event):
    try:
        mode, prompt, input_path, params = request(job)
    except ValueError as error:
        raise BadArguments(str(error)) from error
    started = time.monotonic()
    result, metrics = await asyncio.to_thread(render, mode, prompt, input_path, params, cancelled)
    timings = {**result["timings_ms"], "render": int((time.monotonic() - started) * 1000)}
    outputs = {
        "image": {"kind": "media", "content_type": result["content_type"],
                  "data_base64": result["data_base64"]},
        "metadata": {"kind": "json", "value": {
            "mode": result["mode"], "width": result["width"], "height": result["height"],
            "seed": result["seed"], "timings_ms": result["timings_ms"],
        }},
    }
    return outputs, timings, None, metrics


if __name__ == "__main__":
    prepare_backend()
    serve(ENGINE, ENGINE_VERSION, run)
