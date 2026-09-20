"""引擎子进程：JSON-lines over stdio（protocol/schemas/engine-io.schema.json）。

一个引擎一个进程；同一进程内多个在途 run 以 id 配对。卸载 = 结束进程。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass

from .errors import ApiError
from .registry import EngineSpec

log = logging.getLogger("mmp.engine")


class EngineCrashed(Exception):
    pass


class EngineCancelled(Exception):
    """引擎确认取消（error{cancelled}）。"""


@dataclass
class EngineResult:
    result: object
    timings_ms: dict


class EngineProcess:
    def __init__(self, spec: EngineSpec, start_timeout: float, protocol_version: str):
        self.spec = spec
        self.start_timeout = start_timeout
        self.protocol_version = protocol_version
        self.proc: asyncio.subprocess.Process | None = None
        self.hello: dict | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._seq = 0
        self._reader: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None
        self.last_used = time.monotonic()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    @property
    def busy(self) -> int:
        return len(self._pending)

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.spec.cmd, cwd=str(self.spec.cwd),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024,
        )
        self._stderr = asyncio.create_task(self._pump_stderr())
        try:
            line = await asyncio.wait_for(self.proc.stdout.readline(), self.start_timeout)
        except asyncio.TimeoutError:
            await self.kill()
            raise ApiError("engine_failed", f"engine {self.spec.name} did not say hello within {self.start_timeout}s")
        if not line:
            await self.kill()
            raise ApiError("engine_failed", f"engine {self.spec.name} exited before hello")
        hello = json.loads(line)
        if hello.get("type") != "hello":
            await self.kill()
            raise ApiError("engine_failed", f"engine {self.spec.name} first line was not hello: {hello!r}")
        want = {t["engine_version"] for (_, _), t in self.spec.tiers.items() if t["engine"] == hello.get("engine")}
        if want and hello.get("engine_version") not in want:
            await self.kill()
            raise ApiError("engine_failed",
                           f"engine {self.spec.name} version {hello.get('engine_version')!r} != registry {sorted(want)}")
        self.hello = hello
        self._reader = asyncio.create_task(self._pump_stdout())
        log.info("engine %s started pid=%s version=%s", self.spec.name, self.proc.pid, hello.get("engine_version"))

    async def _pump_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        async for raw in self.proc.stderr:
            log.info("[%s] %s", self.spec.name, raw.decode(errors="replace").rstrip())

    async def _pump_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            async for raw in self.proc.stdout:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("[%s] non-json stdout: %r", self.spec.name, raw[:200])
                    continue
                t = msg.get("type")
                if t == "log":
                    log.log(logging.getLevelName(msg.get("level", "info").upper()), "[%s] %s", self.spec.name, msg.get("message"))
                    continue
                fut = self._pending.pop(msg.get("id"), None)
                if fut is None or fut.done():
                    continue
                if t == "result":
                    fut.set_result(EngineResult(result=msg.get("result"), timings_ms=msg.get("timings_ms") or {}))
                elif t == "error":
                    code = msg.get("error", "engine_failed")
                    if code == "cancelled":
                        fut.set_exception(EngineCancelled())
                    else:
                        fut.set_exception(ApiError("bad_request" if code == "bad_params" else "engine_failed",
                                                   msg.get("message") or code))
        finally:
            # 进程结束：所有在途任务失败
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(EngineCrashed(f"engine {self.spec.name} exited rc={self.proc.returncode if self.proc else '?'}"))
            self._pending.clear()

    async def _send(self, msg: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()

    async def run(self, job: dict, timeout_sec: float) -> EngineResult:
        """超时抛 asyncio.TimeoutError（调用方决定回落还是 504）；引擎错误抛 ApiError；进程死抛 EngineCrashed。"""
        if not self.alive:
            raise EngineCrashed(f"engine {self.spec.name} is not running")
        self._seq += 1
        rid = f"{job['job_id']}#{self._seq}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self.last_used = time.monotonic()
        await self._send({"type": "run", "id": rid, "job": {**job, "timeout_sec": timeout_sec}})
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout_sec)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            if self.alive:
                await self._send({"type": "cancel", "id": rid})
            raise
        except asyncio.CancelledError:
            # 调用方取消（DELETE）：告诉引擎，然后向上传播
            self._pending.pop(rid, None)
            if self.alive:
                await self._send({"type": "cancel", "id": rid})
            raise
        finally:
            self.last_used = time.monotonic()

    async def shutdown(self, grace: float = 5) -> None:
        if not self.alive:
            await self._cleanup()
            return
        try:
            await self._send({"type": "shutdown"})
            await asyncio.wait_for(self.proc.wait(), grace)
            await self._cleanup()
        except Exception:
            await self.kill()

    async def kill(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        await self._cleanup()

    async def _cleanup(self) -> None:
        """显式关管道与泵任务；否则 3.12 的 BaseSubprocessTransport.__del__ 会在循环关闭后报错。"""
        for t in (self._reader, self._stderr):
            if t is not None and not t.done():
                t.cancel()
        if self.proc is not None and self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
                await self.proc.stdin.wait_closed()
            except Exception:
                pass
        transport = getattr(self.proc, "_transport", None)
        if transport is not None:
            transport.close()


class EnginePool:
    """按需起、空闲杀。start 的并发由锁保护，避免同一引擎起两个进程。"""

    def __init__(self, start_timeout: float, idle_unload_sec: float, protocol_version: str):
        self.start_timeout = start_timeout
        self.idle_unload_sec = idle_unload_sec
        self.protocol_version = protocol_version
        self._procs: dict[str, EngineProcess] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._reaper: asyncio.Task | None = None

    def loaded(self) -> list[str]:
        return sorted(n for n, p in self._procs.items() if p.alive)

    async def get(self, spec: EngineSpec) -> EngineProcess:
        lock = self._locks.setdefault(spec.name, asyncio.Lock())
        async with lock:
            p = self._procs.get(spec.name)
            if p is None or not p.alive:
                p = EngineProcess(spec, self.start_timeout, self.protocol_version)
                await p.start()
                self._procs[spec.name] = p
            if self._reaper is None:
                self._reaper = asyncio.create_task(self._reap_loop())
            return p

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(max(1.0, min(30.0, self.idle_unload_sec / 4)))
            now = time.monotonic()
            for name, p in list(self._procs.items()):
                if p.alive and p.busy == 0 and now - p.last_used > self.idle_unload_sec:
                    log.info("engine %s idle for %.0fs, unloading", name, now - p.last_used)
                    await p.shutdown()

    async def close(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        await asyncio.gather(*(p.shutdown() for p in self._procs.values()), return_exceptions=True)
        self._procs.clear()
