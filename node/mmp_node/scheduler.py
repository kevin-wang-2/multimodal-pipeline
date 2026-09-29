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
from . import schemas, transcode

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
        self._vram_reservations: dict[str, int] = {}
        self._resource_changed = asyncio.Event()
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task | None = None
        self._durations: dict[tuple[str, str], float] = {}  # 移动平均，供 eta
        self._stopping = False

    # ---- 生命周期 ----
    def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._loop(), name="mmp-scheduler")

    def begin_shutdown(self) -> None:
        """立刻把未完成任务标成节点下线，避免引擎子进程先退出时误报 engine_failed。"""
        if self._stopping:
            return
        self._stopping = True
        offline = ApiError("node_offline", "node is shutting down")
        for job in list(self._queued.values()):
            self.media.unpin(job.media_id)
            job.finish("failed", offline)
        self._queued.clear()
        self._heap.clear()
        for job in list(self._running.values()):
            if job.task:
                job.task.cancel()
        self._wake.set()

    async def stop(self) -> None:
        self.begin_shutdown()
        loop_task, self._loop_task = self._loop_task, None
        if loop_task:
            loop_task.cancel()
            await asyncio.gather(loop_task, return_exceptions=True)
        tasks = [j.task for j in list(self._running.values()) if j.task]
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
        if self._stopping:
            raise ApiError("node_offline", "node is shutting down")
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
        usable = max(0, self.cfg.scheduler.vram_total_mb - self.cfg.scheduler.vram_headroom_mb)
        return vram == 0 or self._vram_used + vram <= usable

    def _acquire(self, job: Job) -> None:
        t = job.tier
        self._slots[(t["engine"], t["tier"])] = self._slots.get((t["engine"], t["tier"]), 0) + 1
        vram = t.get("vram_mb", 0)
        self._vram_used += vram
        self._vram_reservations[job.job_id] = vram

    def _release(self, job: Job) -> None:
        t = job.tier
        self._slots[(t["engine"], t["tier"])] -= 1
        self._vram_used -= self._vram_reservations.pop(job.job_id, t.get("vram_mb", 0))
        self._resource_changed.set()

    async def _switch_vram_reservation(self, job: Job, vram_mb: int) -> None:
        """内部多阶段任务先释放上一阶段，再等待新阶段预算；不改变对外 tier 或缓存键。"""
        old = self._vram_reservations.get(job.job_id, 0)
        self._vram_used -= old
        self._vram_reservations[job.job_id] = 0
        self._resource_changed.set()
        self._wake.set()
        usable = max(0, self.cfg.scheduler.vram_total_mb - self.cfg.scheduler.vram_headroom_mb)
        while self._vram_used + vram_mb > usable:
            self._resource_changed.clear()
            if self._vram_used + vram_mb <= usable:
                break
            await self._resource_changed.wait()
        self._vram_used += vram_mb
        self._vram_reservations[job.job_id] = vram_mb

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
        actual_source: dict | None = None
        internal_retry_used = False
        fallback_reason: str | None = None
        phase_timings: dict[str, int] = {}
        phase_metrics: dict[str, float] = {}
        try:
            media_path = await self._normalize_media(job)
            spec = self.registry.engine_for(job.task_type, job.tier["tier"])
            while True:
                run_job = {"job_id": job.job_id, "task_type": job.task_type, "tier": job.tier["tier"],
                           "media_id": job.media_id, "params": job.params}
                if fallback_reason:
                    run_job["internal"] = {"fallback_reason": fallback_reason}
                if media_path is not None:
                    run_job["media_path"] = str(media_path)
                stage_vram = self._vram_reservations.get(job.job_id, job.tier.get("vram_mb", 0))
                try:
                    res = await self.pool.run(spec, run_job, spec.timeout_sec, stage_vram)
                    if res.retry is not None:
                        if internal_retry_used:
                            raise ApiError("engine_failed", "engine requested more than one internal retry")
                        target = self.registry.engines.get(str(res.retry.get("engine")))
                        if target is None or target.internal_source is None or target.run_vram_mb <= 0:
                            raise ApiError("engine_failed", f"invalid internal retry engine {res.retry.get('engine')!r}")
                        internal_retry_used = True
                        phase_timings.update({f"base_{k}": int(v) for k, v in res.timings_ms.items()})
                        phase_metrics.update({f"base_{k}": float(v) for k, v in (res.metrics or {}).items()})
                        reason = str(res.retry.get("reason") or "quality_failure")
                        await self._switch_vram_reservation(job, target.run_vram_mb)
                        spec = target
                        fallback_reason = reason
                        actual_source = dict(target.internal_source)
                        degraded_reason = "partial_failure"
                        continue
                    break
                except asyncio.TimeoutError:
                    cheaper = self.registry.cheaper_tier(job.task_type, job.tier["tier"])
                    if cheaper is None:
                        raise ApiError("timeout", f"{job.task_type}@{job.tier['tier']} exceeded {spec.timeout_sec}s, no cheaper tier")
                    log.warning("job %s timed out on %s, falling back to %s", job.job_id, job.tier["tier"], cheaper["tier"])
                    self._release(job)
                    job.tier = cheaper
                    self._acquire(job)
                    spec = self.registry.engine_for(job.task_type, job.tier["tier"])
                    degraded_reason = "timeout"
            final_timings = {k: int(v) for k, v in res.timings_ms.items()}
            if internal_retry_used:
                phase_timings.update({f"large_{k}": v for k, v in final_timings.items()})
                final_timings = phase_timings
                if isinstance(res.result, dict):
                    res.result["timings_ms"] = dict(final_timings)
                phase_metrics.update({f"large_{k}": float(v) for k, v in (res.metrics or {}).items()})
            else:
                phase_metrics.update({k: float(v) for k, v in (res.metrics or {}).items()})
            job.engine_metrics.update(phase_metrics)
            job.timings_ms.update(final_timings)
            job.timings_ms["total"] = int((time.monotonic() - t0) * 1000)
            job.source = {
                "tier": job.tier["tier"], "engine": job.tier["engine"], "engine_version": job.tier["engine_version"],
                "generated_at": _now_iso(), "degraded": degraded_reason is not None, "params": job.params,
            }
            if actual_source:
                job.source.update(actual_source)
                job.source["params"] = {**job.params, "fallback_reason": fallback_reason}
            if degraded_reason:
                job.source["degraded_reason"] = degraded_reason
            result = self._finish_result(job, res.result)
            job.agent_context = self._agent_context(job, result)
            await self._place_result(job, result)
            cache_result = (result if self.registry.capability(job.task_type)["output"]["agent_context"] != "none"
                            else job.result)
            self.cache.put(task_key(job.media_id, job.task_type, job.tier["tier"], job.tier["engine_version"], job.params),
                           job.media_id, job.task_type, job.tier["tier"], job.tier["engine_version"],
                           job.source, cache_result, job.result_ref, job.timings_ms)
            job.finish("done")
            self._durations[(job.tier["engine"], job.tier["tier"])] = (
                0.7 * self._durations.get((job.tier["engine"], job.tier["tier"]), (time.monotonic() - t0)) + 0.3 * (time.monotonic() - t0))
        except (asyncio.CancelledError, EngineCancelled):
            if self._stopping:
                job.finish("failed", ApiError("node_offline", "node is shutting down"))
            else:
                job.finish("cancelled")
        except ApiError as e:
            job.finish("failed", e)
        except EngineCrashed as e:
            code = "node_offline" if self._stopping else "engine_failed"
            job.finish("failed", ApiError(code, str(e)))
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

    async def _normalize_media(self, job: Job):
        """声明 audio.to_wav_16k_mono 预处理的任务：非 PCM WAV 先归一化。"""
        if job.media_path is None or not self.cfg.media.transcode_audio:
            return job.media_path
        media = self.registry.capability(job.task_type)["input"]["media"]
        if media.get("preprocessor") != "audio.to_wav_16k_mono" or not transcode.needs_transcode(job.media_path):
            return job.media_path
        dst = self.media.derived_path(job.media_id, "16k.wav")
        if dst.exists():
            job.timings_ms["transcode"] = 0
            return dst
        ffmpeg = transcode.find_ffmpeg(self.cfg.media.ffmpeg)
        if ffmpeg is None:
            raise ApiError("bad_request", "audio is not PCM WAV and this node has no ffmpeg to transcode it; submit 16 kHz mono WAV")
        t0 = time.monotonic()
        await transcode.to_wav16k(job.media_path, dst, ffmpeg, self.cfg.media.transcode_timeout_sec)
        job.timings_ms["transcode"] = int((time.monotonic() - t0) * 1000)
        log.info("transcoded %s → %s in %d ms", job.media_id, dst.name, job.timings_ms["transcode"])
        return dst

    def _finish_result(self, job: Job, result: object) -> object:
        """digest 类型：A 补 source、按 tools 判部分失败；所有类型：按 output.schema 校验。"""
        out_schema = self.registry.result_schema(job.task_type)
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
                raise ApiError("engine_failed", f"engine output violates {out_schema if isinstance(out_schema, str) else 'output.schema'}: " + "; ".join(errs[:3]))
        return result

    def _agent_context(self, job: Job, result: object) -> dict | None:
        context = self.registry.present_agent_context(job.task_type, result)
        if context is None:
            return None
        v = schemas.validator_for_ref("urn:mmp:protocol:2:job-api#/$defs/AgentContext")
        errs = schemas.errors(v, context)
        if errs:
            raise ApiError("engine_failed", "agent_context violates protocol: " + "; ".join(errs[:3]))
        return context

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
