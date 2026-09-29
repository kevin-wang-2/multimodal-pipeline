"""引擎侧的 engine-io 循环，只依赖标准库；每个引擎子进程 import 它。

用法：
    from engines.common.io import serve
    async def run(job: dict, cancelled: asyncio.Event) -> tuple[object, dict]: ...
    serve(engine="echo", engine_version="1", run=run)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import traceback
from typing import Awaitable, Callable

PROTOCOL_VERSION = "1.2"
RunFn = Callable[[dict, asyncio.Event], Awaitable[tuple]]

# 协议帧只走原始 stdout 的字节流；之后把 sys.stdout 指到 stderr，第三方库的 print 就污染不了 JSON 流
_PROTO_OUT = sys.stdout.buffer
sys.stdout = sys.stderr


class BadParams(Exception):
    pass


def _emit(msg: dict) -> None:
    # 直接写字节：Windows 上 sys.stdout 的文本层可能是 GBK，且会把 \n 翻成 \r\n
    _PROTO_OUT.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
    _PROTO_OUT.flush()


def log(message: str, level: str = "info") -> None:
    _emit({"type": "log", "level": level, "message": message})


async def _serve(engine: str, engine_version: str, run: RunFn) -> None:
    _emit({"type": "hello", "engine": engine, "engine_version": engine_version, "protocol_version": PROTOCOL_VERSION})
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[bytes] = asyncio.Queue()

    def pump() -> None:
        # 线程读 stdin：asyncio 的 connect_read_pipe 在 Windows Proactor 上对匿名管道不可用。
        # 用 os.read 裸读 fd 0 而不是 sys.stdin.buffer：daemon 线程若在解释器退出时还持有
        # BufferedReader 的锁，Python 会 fatal_error（SIGABRT）。裸 fd 没有锁；A 关 stdin 时 read 返回空串。
        buf = b""
        while True:
            try:
                chunk = os.read(0, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                break
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                loop.call_soon_threadsafe(lines.put_nowait, buf[: nl + 1])
                buf = buf[nl + 1:]
        if buf:
            loop.call_soon_threadsafe(lines.put_nowait, buf)
        loop.call_soon_threadsafe(lines.put_nowait, b"")

    threading.Thread(target=pump, name="stdin-pump", daemon=True).start()
    inflight: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}

    async def handle(rid: str, job: dict, cancelled: asyncio.Event) -> None:
        try:
            outcome = await run(job, cancelled)
            result, timings = outcome[:2]
            retry = outcome[2] if len(outcome) > 2 else None
            metrics = outcome[3] if len(outcome) > 3 else None
            if cancelled.is_set():
                _emit({"type": "error", "id": rid, "error": "cancelled"})
            else:
                msg = {"type": "result", "id": rid, "timings_ms": timings}
                if retry is None:
                    msg["result"] = result
                else:
                    msg["retry"] = retry
                if metrics:
                    msg["metrics"] = metrics
                _emit(msg)
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
        raw = await lines.get()
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
