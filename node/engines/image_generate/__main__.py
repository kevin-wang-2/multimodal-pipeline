import asyncio
import time

from engines.common.io import BadArguments, serve
from engines.image_generate import ENGINE, ENGINE_VERSION
from engines.image_generate.runner import generate, prepare_backend, request


async def run(job: dict, cancelled: asyncio.Event):
    try:
        prompt, params = request(job)
    except ValueError as e:
        raise BadArguments(str(e)) from e
    started = time.monotonic()
    result, metrics = await asyncio.to_thread(generate, prompt, params, cancelled)
    timings = {"generate": int((time.monotonic() - started) * 1000)}
    outputs = {
        "image": {"kind": "media", "content_type": result["content_type"], "data_base64": result["data_base64"]},
        "metadata": {"kind": "json", "value": {
            "width": result["width"], "height": result["height"], "seed": result["seed"], "timings_ms": timings,
        }},
    }
    return outputs, timings, None, metrics


if __name__ == "__main__":
    # Windows 下先初始化原生库，再启动阻塞 stdin 读取线程和推理工作线程。
    prepare_backend()
    serve(ENGINE, ENGINE_VERSION, run)
