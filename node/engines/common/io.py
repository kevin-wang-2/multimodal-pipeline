"""引擎侧的 engine-io 循环，只依赖标准库；每个引擎子进程 import 它。

用法：
    from engines.common.io import serve
    async def run(job: dict, cancelled: asyncio.Event) -> tuple[object, dict]: ...
    serve(engine="echo", engine_version="1", run=run)
"""
from __future__ import annotations

import asyncio
import json
import sys
import traceback
from typing import Awaitable, Callable

PROTOCOL_VERSION = "1.0"
RunFn = Callable[[dict, asyncio.Event], Awaitable[tuple[object, dict]]]


class BadParams(Exception):
    pass


def _emit(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def log(message: str, level: str = "info") -> None:
    _emit({"type": "log", "level": level, "message": message})


async def _serve(engine: str, engine_version: str, run: RunFn) -> None:
    _emit({"type": "hello", "engine": engine, "engine_version": engine_version, "protocol_version": PROTOCOL_VERSION})
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    inflight: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}

    async def handle(rid: str, job: dict, cancelled: asyncio.Event) -> None:
        try:
            result, timings = await run(job, cancelled)
            if cancelled.is_set():
                _emit({"type": "error", "id": rid, "error": "cancelled"})
            else:
                _emit({"type": "result", "id": rid, "result": result, "timings_ms": timings})
        except asyncio.CancelledError:
            _emit({"type": "error", "id": rid, "error": "cancelled"})
        except BadParams as e:
            _emit({"type": "error", "id": rid, "error": "bad_params", "message": str(e)})
        except Exception as e:
            _emit({"type": "error", "id": rid, "error": "engine_failed", "message": f"{type(e).__name__}: {e}"})
            log(traceback.format_exc(), "error")
        finally:
            inflight.pop(rid, None)

    while True:
        raw = await reader.readline()
        if not raw:
            break
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log(f"bad line: {raw[:200]!r}", "warning")
            continue
        t = msg.get("type")
        if t == "run":
            ev = asyncio.Event()
            inflight[msg["id"]] = (asyncio.create_task(handle(msg["id"], msg["job"], ev)), ev)
        elif t == "cancel":
            entry = inflight.get(msg.get("id"))
            if entry:
                entry[1].set()
                entry[0].cancel()
        elif t == "shutdown":
            break
    for task, _ in inflight.values():
        task.cancel()


def serve(engine: str, engine_version: str, run: RunFn) -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(_serve(engine, engine_version, run))
