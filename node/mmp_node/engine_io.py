"""引擎子进程：JSON-lines over stdio（protocol/schemas/engine-io.schema.json）。

一个引擎一个进程；同一进程内多个在途 run 以 id 配对。卸载 = 结束进程。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass

from .errors import ApiError
from .registry import EngineSpec

log = logging.getLogger("mmp.engine")


class EngineCrashed(Exception):
    pass


class EngineCancelled(Exception):
    """引擎确认取消（error{cancelled}）。"""


class EngineTimeout(asyncio.TimeoutError):
    """run 已发出但未按时返回；compute_ms 是本次失败尝试实际占用引擎的时间。"""

    def __init__(self, compute_ms: int):
        super().__init__()
        self.compute_ms = compute_ms


@dataclass
class EngineResult:
    result: object | None
    timings_ms: dict
    retry: dict | None = None
    metrics: dict | None = None
    compute_ms: int = 0


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
            # 子进程 stdio 一律 utf-8：Windows 默认 GBK 会把 JSON 里的中文编坏
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", **self.spec.env},
        )
        self._stderr = asyncio.create_task(self._pump_stderr())
        hello = await self._wait_hello()
        if str(hello.get("protocol_version", "")).split(".")[0] != self.protocol_version.split(".")[0]:
            await self.kill()
            raise ApiError("engine_failed",
                           f"engine {self.spec.name} protocol {hello.get('protocol_version')!r} incompatible with {self.protocol_version}")
        want = {t["engine_version"] for (_, _), t in self.spec.tiers.items() if t["engine"] == hello.get("engine")}
        if want and hello.get("engine_version") not in want:
            await self.kill()
            raise ApiError("engine_failed",
                           f"engine {self.spec.name} version {hello.get('engine_version')!r} != registry {sorted(want)}")
        self.hello = hello
        self._reader = asyncio.create_task(self._pump_stdout())
        log.info("engine %s started pid=%s version=%s", self.spec.name, self.proc.pid, hello.get("engine_version"))

    async def _wait_hello(self) -> dict:
        """等 hello。加载期间引擎可以发 log，第三方库也可能往 stdout 打非 JSON 的东西——都记日志跳过，只有超时 / 退出 / 非 hello 的协议消息才算失败。"""
        assert self.proc and self.proc.stdout
        deadline = asyncio.get_running_loop().time() + self.start_timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await self.kill()
                raise ApiError("engine_failed", f"engine {self.spec.name} did not say hello within {self.start_timeout}s")
            try:
                line = await asyncio.wait_for(self.proc.stdout.readline(), remaining)
            except asyncio.TimeoutError:
                await self.kill()
                raise ApiError("engine_failed", f"engine {self.spec.name} did not say hello within {self.start_timeout}s")
            if not line:
                await self.kill()
                raise ApiError("engine_failed", f"engine {self.spec.name} exited before hello (rc={self.proc.returncode})")
            try:
                msg = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                log.info("[%s] (pre-hello stdout) %s", self.spec.name, line[:200].decode(errors="replace").rstrip())
                continue
            if msg.get("type") == "log":
                log.log(logging.getLevelName(msg.get("level", "info").upper()), "[%s] %s", self.spec.name, msg.get("message"))
                continue
            if msg.get("type") != "hello":
                await self.kill()
                raise ApiError("engine_failed", f"engine {self.spec.name} sent {msg.get('type')!r} before hello")
            return msg

    async def _pump_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        async for raw in self.proc.stderr:
            log.info("[%s] %s", self.spec.name, raw.decode(errors="replace").rstrip())

    async def _pump_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            async for raw in self.proc.stdout:
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    log.warning("[%s] undecodable stdout line (%s): %r", self.spec.name, e, raw[:200])
                    continue
                t = msg.get("type")
                if t == "log":
                    log.log(logging.getLevelName(msg.get("level", "info").upper()), "[%s] %s", self.spec.name, msg.get("message"))
                    continue
                fut = self._pending.pop(msg.get("id"), None)
                if fut is None or fut.done():
                    continue
                if t == "result":
                    fut.set_result(EngineResult(result=msg.get("result"), timings_ms=msg.get("timings_ms") or {},
                                                retry=msg.get("retry"), metrics=msg.get("metrics") or {}))
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
        compute_started = time.monotonic()
        await self._send({"type": "run", "id": rid, "job": {**job, "timeout_sec": timeout_sec}})
        try:
            result = await asyncio.wait_for(asyncio.shield(fut), timeout_sec)
            result.compute_ms = int((time.monotonic() - compute_started) * 1000)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            if self.alive:
                await self._send({"type": "cancel", "id": rid})
            raise EngineTimeout(int((time.monotonic() - compute_started) * 1000)) from None
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
        # Proactor（Windows）把管道的 connection_lost 放到 call_soon；多转两拍让它在 loop 关闭前跑完
        for _ in range(3):
            await asyncio.sleep(0)


class EnginePool:
    """引擎进程池。

    CPU 引擎按需启动并按 idle timeout 回收。GPU 以容量而不是引擎类别调度：活跃任务按
    峰值预留，空闲进程按 resident_vram_mb 占用；只有总量越过安全预算时才驱逐空闲进程。
    keep_warm 是可回收的空闲偏好，任务结束后按优先级把放得下的集合补回来。
    """

    def __init__(self, start_timeout: float, idle_unload_sec: float, protocol_version: str,
                 specs: Iterable[EngineSpec] = (), vram_total_mb: int = 0, vram_headroom_mb: int = 0):
        self.start_timeout = start_timeout
        self.idle_unload_sec = idle_unload_sec
        self.protocol_version = protocol_version
        self._specs = {s.name: s for s in specs}
        self._procs: dict[str, EngineProcess] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._leases: dict[str, int] = {}
        self._gpu_transition = asyncio.Lock()
        self._capacity_changed = asyncio.Condition(self._gpu_transition)
        self._active_vram_mb = 0
        self._usable_vram_mb = max(0, vram_total_mb - vram_headroom_mb)
        self._reaper: asyncio.Task | None = None
        self._warm_task: asyncio.Task | None = None
        self._closing = False

    @staticmethod
    def _is_gpu(spec: EngineSpec) -> bool:
        return bool(spec.resident_vram_mb or spec.run_vram_mb
                    or any(t.get("vram_mb", 0) > 0 for t in spec.tiers.values()))

    def start(self) -> None:
        self._schedule_warm()

    def loaded(self) -> list[str]:
        return sorted(n for n, p in self._procs.items() if p.alive)

    async def _get(self, spec: EngineSpec) -> EngineProcess:
        lock = self._locks.setdefault(spec.name, asyncio.Lock())
        async with lock:
            if self._closing:
                raise EngineCrashed("engine pool is closing")
            p = self._procs.get(spec.name)
            if p is None or not p.alive:
                p = EngineProcess(spec, self.start_timeout, self.protocol_version)
                await p.start()
                self._procs[spec.name] = p
            if self._reaper is None:
                self._reaper = asyncio.create_task(self._reap_loop())
            return p

    async def get(self, spec: EngineSpec) -> EngineProcess:
        """兼容只取进程的内部入口；任务执行应使用 run() 以持有生命周期 lease。"""
        return await self._get(spec)

    async def run(self, spec: EngineSpec, job: dict, timeout_sec: float, vram_mb: int = 0) -> EngineResult:
        if vram_mb:
            async with self._capacity_changed:
                while self._active_vram_mb + vram_mb > self._usable_vram_mb:
                    await self._capacity_changed.wait()
                await self._evict_until_fit(spec.name, vram_mb)
                self._active_vram_mb += vram_mb
                try:
                    proc = await self._get(spec)
                except Exception:
                    self._active_vram_mb -= vram_mb
                    self._capacity_changed.notify_all()
                    raise
                self._leases[spec.name] = self._leases.get(spec.name, 0) + 1
        else:
            proc = await self._get(spec)
            self._leases[spec.name] = self._leases.get(spec.name, 0) + 1
        try:
            return await proc.run(job, timeout_sec)
        finally:
            if vram_mb:
                async with self._capacity_changed:
                    self._leases[spec.name] = max(0, self._leases.get(spec.name, 1) - 1)
                    self._active_vram_mb -= vram_mb
                    if not spec.keep_warm and self._leases[spec.name] == 0:
                        await proc.shutdown()
                    self._capacity_changed.notify_all()
                    self._schedule_warm()
            else:
                self._leases[spec.name] = max(0, self._leases.get(spec.name, 1) - 1)

    def _idle_resident_mb(self, exclude: str | None = None) -> int:
        return sum(
            spec.resident_vram_mb
            for name, spec in self._specs.items()
            if name != exclude and spec.resident_vram_mb and self._leases.get(name, 0) == 0
            and (proc := self._procs.get(name)) is not None and proc.alive and proc.busy == 0
        )

    async def _evict_until_fit(self, wanted: str, additional_vram_mb: int) -> None:
        candidates = []
        for name, proc in self._procs.items():
            spec = self._specs.get(name)
            if (name != wanted and spec is not None and self._is_gpu(spec)
                    and self._leases.get(name, 0) == 0 and proc.busy == 0 and proc.alive):
                candidates.append((1 if spec.keep_warm else 0, spec.warm_priority, proc.last_used, name, proc))
        for _, _, _, name, proc in sorted(candidates):
            needed = self._active_vram_mb + additional_vram_mb + self._idle_resident_mb(exclude=wanted)
            if needed <= self._usable_vram_mb:
                break
            log.info("unloading idle GPU engine %s for %s (%d > %d MiB)",
                     name, wanted, needed, self._usable_vram_mb)
            await proc.shutdown()

    def _schedule_warm(self) -> None:
        if self._closing or not any(s.keep_warm for s in self._specs.values()):
            return
        if self._warm_task is None or self._warm_task.done():
            self._warm_task = asyncio.create_task(self._restore_warm(), name="mmp-engine-warm")

    async def _restore_warm(self) -> None:
        # 让刚结束任务的响应和紧邻的排队任务先推进，避免在交付路径里等待模型加载。
        await asyncio.sleep(0)
        async with self._capacity_changed:
            if self._closing:
                return
            warm_specs = sorted((s for s in self._specs.values() if s.keep_warm),
                                key=lambda s: (-s.warm_priority, s.name))
            for warm in warm_specs:
                proc = self._procs.get(warm.name)
                if proc is not None and proc.alive:
                    continue
                needed = self._active_vram_mb + self._idle_resident_mb() + warm.resident_vram_mb
                if needed > self._usable_vram_mb:
                    continue
                try:
                    await self._get(warm)
                    log.info("keep_warm GPU engine %s is ready", warm.name)
                except Exception:
                    log.exception("failed to restore keep_warm engine %s", warm.name)

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(max(1.0, min(30.0, self.idle_unload_sec / 4)))
            now = time.monotonic()
            for name, p in list(self._procs.items()):
                if p.alive and p.busy == 0 and now - p.last_used > self.idle_unload_sec:
                    spec = self._specs.get(name)
                    if spec is not None and spec.keep_warm:
                        continue
                    log.info("engine %s idle for %.0fs, unloading", name, now - p.last_used)
                    await p.shutdown()

    async def close(self) -> None:
        """等在途的 start 收尾（调度器用 shield 保护它不被 cancel），再关进程。
        否则半起的子进程会留给事件循环收尾，Windows Proactor 上 cancel _connect_pipes 会挂死。"""
        self._closing = True
        if self._reaper:
            self._reaper.cancel()
        if self._warm_task:
            self._warm_task.cancel()
        for lock in list(self._locks.values()):
            async with lock:
                pass
        await asyncio.gather(*(p.shutdown() for p in self._procs.values()), return_exceptions=True)
        self._procs.clear()
