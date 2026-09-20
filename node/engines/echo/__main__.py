import asyncio
import os
import time

from engines.common.io import serve
from engines.echo import ENGINE, ENGINE_VERSION


async def run(job: dict, cancelled: asyncio.Event) -> tuple[object, dict]:
    t0 = time.monotonic()
    started_at_ms = int(time.time() * 1000)
    params = job.get("params") or {}
    size = os.path.getsize(job["media_path"]) if job.get("media_path") else 0
    sleep_ms = int(params.get("sleep_ms", 0))
    if sleep_ms:
        await asyncio.sleep(sleep_ms / 1000)
    if params.get("fail"):
        raise RuntimeError("echo asked to fail")
    result = {"media_id": job["media_id"], "bytes": size, "params": params, "started_at_ms": started_at_ms}
    if params.get("pad_bytes"):
        result["pad"] = "x" * int(params["pad_bytes"])
    return result, {"sleep": sleep_ms, "echo": int((time.monotonic() - t0) * 1000)}


if __name__ == "__main__":
    serve(ENGINE, ENGINE_VERSION, run)
