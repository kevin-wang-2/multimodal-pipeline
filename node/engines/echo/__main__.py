import asyncio
import os
import time

from engines.common.io import serve
from engines.echo import ENGINE, ENGINE_VERSION


async def run(job: dict, cancelled: asyncio.Event) -> tuple[object, dict]:
    t0 = time.monotonic()
    started_at_ms = int(time.time() * 1000)
    arguments = job["arguments"]
    params = arguments.get("options") or {}
    media = arguments["media"]
    size = os.path.getsize(media["path"])
    sleep_ms = int(params.get("sleep_ms", 0))
    if sleep_ms:
        await asyncio.sleep(sleep_ms / 1000)
    if params.get("fail"):
        raise RuntimeError("echo asked to fail")
    result = {"media_id": media["media_id"], "bytes": size, "options": params, "started_at_ms": started_at_ms}
    if params.get("pad_bytes"):
        result["pad"] = "x" * int(params["pad_bytes"])
    return {"result": {"kind": "json", "value": result}}, {"sleep": sleep_ms, "echo": int((time.monotonic() - t0) * 1000)}


if __name__ == "__main__":
    serve(ENGINE, ENGINE_VERSION, run)
