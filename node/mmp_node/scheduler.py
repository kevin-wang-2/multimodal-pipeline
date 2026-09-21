"""优先级队列 + 调度器 + 执行。

- interactive > batch，同优先级按提交顺序；
- 每个 (engine, tier) 的并发上限与 VRAM 预算由注册表声明，调度器按预算挑第一个能跑的；
- 超时：有更便宜的档就回落并标 degraded=timeout，没有就 504 timeout。
"""
from __future__ import annotations

import asyncio
import heapq
import json
import logging
import time
from datetime import datetime, timezone

from .cache import ResultCache, task_key
from .config import Config
from .engine_io import EngineCancelled, EngineCrashed, EnginePool
from .errors import ApiError
from .jobs import PRIORITY_RANK, Job
from .media import MediaStore
from .registry import Registry
from . import schemas

log = logging.getLogger("mmp.sched")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Scheduler:
    def __init__(self, cfg: Config, registry: Registry, pool: EnginePool, cache: ResultCache, media: MediaStore):
        self.cfg = cfg
        self.registry = registry
        self.pool = pool
        self.cache = cache
        self.media = media
        self._heap: list[tuple[int, int, str]] = []
        self._queued: dict[str, Job] = {}
        self._running: dict[str, Job] = {}
        self._slots: dict[tuple[str, str], int] = {}   # (engine, tier) → 在跑数
        self._vram_used = 0
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task | None = None
        self._durations: dict[tuple[str, str], float] = {}  # 移动平均，供 eta

    # ---- 生命周期 ----
    def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._loop(), name="mmp-scheduler")

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            self._loop_task = None
        tasks = [j.task for j in list(self._running.values()) if j.task]
        for t in tasks:
            t.cancel()
        # 等任务真正收尾（它们的 except/finally 还会跟引擎进程说话），再让 EnginePool 关进程
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ---- 队列 ----
    @property
    def queue_len(self) -> int:
        return len(self._heap)

    @property
    def running(self) -> int:
        return len(self._running)

    def check_backpressure(self) -> None:
        if self.queue_len >= self.cfg.queue.max_len:
            raise ApiError("backpressure", f"queue full ({self.queue_len})", retry_after_sec=self.cfg.queue.retry_after_sec)

    def find_active(self, key: str) -> Job | None:
        for j in list(self._queued.values()) + list(self._running.values()):
            if j.key == key:
                return j
        return None

    def enqueue(self, job: Job) -> None:
        self.check_backpressure()
        heapq.heappush(self._heap, (PRIORITY_RANK[job.priority], job.seq, job.job_id))
        self._queued[job.job_id] = job
        self._refresh_positions()
        self._wake.set()

    def cancel(self, job: Job) -> None:
        if job.job_id in self._queued:
            del self._queued[job.job_id]
            self._heap = [e for e in self._heap if e[2] != job.job_id]
            heapq.heapify(self._heap)
            self._refresh_positions()
            self.media.unpin(job.media_id)
            job.finish("cancelled")
        elif job.job_id in self._running:
            job.cancel_requested = True
            if job.task:
                job.task.cancel()

    def _refresh_positions(self) -> None:
        for pos, (_, _, jid) in enumerate(sorted(self._heap)):
            j = self._queued.get(jid)
            if j:
                j.queue_position = pos
                avg = self._durations.get((j.tier["engine"], j.tier["tier"]))
                j.eta_sec = round(avg * (pos + 1), 1) if avg else None

    # ---- 调度 ----
    def _can_run(self, job: Job) -> bool:
        t = job.tier
        slot = (t["engine"], t["tier"])
        if self._slots.get(slot, 0) >= t.get("max_concurrency", 1):
            return False
        vram = t.get("vram_mb", 0)
        return vram == 0 or self._vram_used + vram <= self.cfg.scheduler.vram_total_mb

    def _acquire(self, job: Job) -> None:
        t = job.tier
        self._slots[(t["engine"], t["tier"])] = self._slots.get((t["engine"], t["tier"]), 0) + 1
        self._vram_used += t.get("vram_mb", 0)

    def _release(self, job: Job) -> None:
        t = job.tier
        self._slots[(t["engine"], t["tier"])] -= 1
        self._vram_used -= t.get("vram_mb", 0)

    async def _loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            started = True
            while started:
                started = False
                for entry in sorted(self._heap):
                    job = self._queued.get(entry[2])
                    if job is None or not self._can_run(job):
                        continue
                    self._heap.remove(entry)
                    heapq.heapify(self._heap)
                    del self._queued[job.job_id]
                    self._acquire(job)
                    self._running[job.job_id] = job
                    job.task = asyncio.create_task(self._run(job), name=f"job-{job.job_id}")
                    started = True
                    break
            self._refresh_positions()

    # ---- 执行 ----
    async def _run(self, job: Job) -> None:
        job.status = "running"
        job.started_at = time.time()
        job.queue_position = None
        t0 = time.monotonic()
        degraded_reason: str | None = None
        try:
            while True:
                spec = self.registry.engine_for(job.task_type, job.tier["tier"])
                proc = await asyncio.shield(self.pool.get(spec))
                run_job = {"job_id": job.job_id, "task_type": job.task_type, "tier": job.tier["tier"],
                           "media_id": job.media_id, "params": job.params}
                if job.media_path is not None:
                    run_job["media_path"] = str(job.media_path)
                try:
                    res = await proc.run(run_job, spec.timeout_sec)
                    break
                except asyncio.TimeoutError:
                    cheaper = self.registry.cheaper_tier(job.task_type, job.tier["tier"])
                    if cheaper is None:
                        raise ApiError("timeout", f"{job.task_type}@{job.tier['tier']} exceeded {spec.timeout_sec}s, no cheaper tier")
                    log.warning("job %s timed out on %s, falling back to %s", job.job_id, job.tier["tier"], cheaper["tier"])
                    self._release(job)
                    job.tier = cheaper
                    self._acquire(job)
                    degraded_reason = "timeout"
            job.timings_ms.update({k: int(v) for k, v in res.timings_ms.items()})
            job.timings_ms["total"] = int((time.monotonic() - t0) * 1000)
            job.source = {
                "tier": job.tier["tier"], "engine": job.tier["engine"], "engine_version": job.tier["engine_version"],
                "generated_at": _now_iso(), "degraded": degraded_reason is not None, "params": job.params,
            }
            if degraded_reason:
                job.source["degraded_reason"] = degraded_reason
            result = self._finish_result(job, res.result)
            await self._place_result(job, result)
            self.cache.put(task_key(job.media_id, job.task_type, job.tier["tier"], job.tier["engine_version"], job.params),
                           job.media_id, job.task_type, job.tier["tier"], job.tier["engine_version"],
                           job.source, job.result, job.result_ref, job.timings_ms)
            job.finish("done")
            self._durations[(job.tier["engine"], job.tier["tier"])] = (
                0.7 * self._durations.get((job.tier["engine"], job.tier["tier"]), (time.monotonic() - t0)) + 0.3 * (time.monotonic() - t0))
        except (asyncio.CancelledError, EngineCancelled):
            job.finish("cancelled")
        except ApiError as e:
            job.finish("failed", e)
        except EngineCrashed as e:
            job.finish("failed", ApiError("engine_failed", str(e)))
        except Exception as e:  # 不让任何异常把调度器带走
            log.exception("job %s crashed in scheduler", job.job_id)
            job.finish("failed", ApiError("engine_failed", f"{type(e).__name__}: {e}"))
        finally:
            self._release(job)
            self._running.pop(job.job_id, None)
            self.media.unpin(job.media_id)
            self._wake.set()
            log.info(json.dumps({"job": job.job_id, "type": job.task_type, "status": job.status,
                                 "tier": job.tier["tier"], "degraded": degraded_reason, "cached": False,
                                 "timings_ms": job.timings_ms}, ensure_ascii=False))

    def _finish_result(self, job: Job, result: object) -> object:
        """digest 类型：A 补 source、按 tools 判部分失败；所有类型：按 output_schema 校验，不合格算 engine_failed。"""
        out_schema = self.registry.output_schema(job.task_type)
        if out_schema == schemas.DIGEST_ID and isinstance(result, dict):
            tools = result.get("tools") or {}
            if any(v in ("failed", "partial") for v in tools.values()) and not job.source["degraded"]:
                job.source["degraded"] = True
                job.source["degraded_reason"] = "partial_failure"
            result["source"] = job.source
        v = schemas.validator_for(out_schema)
        if v is not None:
            errs = schemas.errors(v, result)
            if errs:
                raise ApiError("engine_failed", f"engine output violates {out_schema if isinstance(out_schema, str) else 'output_schema'}: " + "; ".join(errs[:3]))
        return result

    async def _place_result(self, job: Job, result: object) -> None:
        """结果内联；超限且有 put 端点则 PUT 过去只留引用。"""
        blob = json.dumps(result, ensure_ascii=False).encode()
        put = job.media.get("put")
        if len(blob) <= self.cfg.media.max_inline_result_bytes or put is None:
            if len(blob) > self.cfg.media.max_inline_result_bytes:
                raise ApiError("engine_failed", f"result is {len(blob)} bytes > inline limit and no put endpoint given")
            job.result = result
            return
        import hashlib
        await self.media.put(put, blob, "application/json")
        job.result_ref = {"content_type": "application/json", "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
