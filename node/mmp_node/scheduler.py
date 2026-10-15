"""优先级队列 + 调度器 + 执行。

- interactive > batch，同优先级按提交顺序；
- 每个 (engine, tier) 的并发上限与 VRAM 预算由注册表声明，调度器按预算挑第一个能跑的；
- 超时：有更便宜的档就回落并标 degraded=timeout，没有就 504 timeout。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import heapq
import json
import logging
import time
from datetime import datetime, timezone

from .cache import ResultCache
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
            self._unpin(job)
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

    def check_backpressure(self, preparing: int = 0) -> None:
        if self._stopping:
            raise ApiError("node_offline", "node is shutting down")
        if self.queue_len + preparing >= self.cfg.queue.max_len:
            raise ApiError("backpressure", f"queue full ({self.queue_len + preparing})", retry_after_sec=self.cfg.queue.retry_after_sec)

    def find_active(self, key: str, delivery: dict) -> Job | None:
        for j in list(self._queued.values()) + list(self._running.values()):
            if j.key == key and j.delivery == delivery:
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
            self._unpin(job)
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
        wasted_ms = 0
        try:
            await self._normalize_media(job)
            spec = self.registry.engine_for(job.task_type, job.tier["tier"])
            while True:
                run_job = {"job_id": job.job_id, "task_type": job.task_type, "tier": job.tier["tier"],
                           "arguments": job.arguments}
                if fallback_reason:
                    run_job["internal"] = {"fallback_reason": fallback_reason}
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
                        wasted_ms += res.compute_ms
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
                except asyncio.TimeoutError as e:
                    wasted_ms += int(getattr(e, "compute_ms", 0))
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
                if isinstance(res.outputs, dict):
                    for output in res.outputs.values():
                        if isinstance(output, dict) and output.get("kind") == "json" and isinstance(output.get("value"), dict):
                            output["value"]["timings_ms"] = dict(final_timings)
                phase_metrics.update({f"large_{k}": float(v) for k, v in (res.metrics or {}).items()})
            else:
                phase_metrics.update({k: float(v) for k, v in (res.metrics or {}).items()})
            job.engine_metrics.update(phase_metrics)
            job.timings_ms.update(final_timings)
            job.timings_ms["total"] = int((time.monotonic() - t0) * 1000)
            media_fields = {field["name"] for field in self.registry.capability(job.task_type)["input"]["fields"]
                            if field["kind"] == "media"}
            job.source = {
                "tier": job.tier["tier"], "engine": job.tier["engine"], "engine_version": job.tier["engine_version"],
                "generated_at": _now_iso(), "degraded": degraded_reason is not None,
                "arguments": {name: value for name, value in job.arguments.items() if name not in media_fields},
            }
            if actual_source:
                job.source.update(actual_source)
                job.source["arguments"] = {**job.source["arguments"], "fallback_reason": fallback_reason}
            if degraded_reason:
                job.source["degraded_reason"] = degraded_reason
            job.usage = {"served_from": "compute", "tier": job.source["tier"], "engine": job.source["engine"],
                         "compute_ms": res.compute_ms, "wasted_ms": wasted_ms}
            raw_outputs = self._finish_outputs(job, res.outputs or {})
            job.raw_outputs = raw_outputs
            job.agent_context = self._agent_context(job, raw_outputs)
            self.cache.put(job.key, job.media_ids, job.task_type, job.tier["tier"], job.tier["engine_version"],
                           job.source, raw_outputs, job.timings_ms)
            await self._place_outputs(job, raw_outputs)
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
            self._unpin(job)
            self._wake.set()
            log.info(json.dumps({"job": job.job_id, "type": job.task_type, "status": job.status,
                                 "tier": job.tier["tier"], "degraded": degraded_reason, "cached": False,
                                 "usage": job.usage, "timings_ms": job.timings_ms}, ensure_ascii=False))

    async def _normalize_media(self, job: Job) -> None:
        """对声明 audio.to_wav_16k_mono 的每个媒体字段做预处理。"""
        if not self.cfg.media.transcode_audio:
            return
        fields = {field["name"]: field for field in self.registry.capability(job.task_type)["input"]["fields"]}
        for name, field in fields.items():
            if field["kind"] != "media" or field.get("preprocessor") != "audio.to_wav_16k_mono":
                continue
            values = job.arguments.get(name)
            items = values if isinstance(values, list) else [values]
            for item in [item for item in items if isinstance(item, dict)]:
                path = item.get("path")
                if not path or not transcode.needs_transcode(path):
                    continue
                dst = self.media.derived_path(item["media_id"], "16k.wav")
                if dst.exists():
                    job.timings_ms[f"transcode.{name}"] = 0
                else:
                    ffmpeg = transcode.find_ffmpeg(self.cfg.media.ffmpeg)
                    if ffmpeg is None:
                        raise ApiError("bad_request", "audio is not PCM WAV and this node has no ffmpeg to transcode it; submit 16 kHz mono WAV")
                    t0 = time.monotonic()
                    await transcode.to_wav16k(path, dst, ffmpeg, self.cfg.media.transcode_timeout_sec)
                    job.timings_ms[f"transcode.{name}"] = int((time.monotonic() - t0) * 1000)
                item["path"] = str(dst)

    def _finish_outputs(self, job: Job, outputs: dict) -> dict:
        """校验命名输出；digest 在 A 补来源与可用能力。"""
        fields = {field["name"]: field for field in self.registry.capability(job.task_type)["output"]["fields"]}
        if extra := set(outputs) - set(fields):
            raise ApiError("engine_failed", f"engine returned unknown outputs {sorted(extra)}")
        for name, field in fields.items():
            if field["required"] and name not in outputs:
                raise ApiError("engine_failed", f"engine omitted required output {name!r}")
            if name not in outputs:
                continue
            output = outputs[name]
            many = field["kind"] == "media" and field["cardinality"]["max"] != 1
            if many and not isinstance(output, list):
                raise ApiError("engine_failed", f"engine output {name!r} must be an array")
            values = output if many and isinstance(output, list) else [output]
            if many and not field["cardinality"]["min"] <= len(values) <= field["cardinality"]["max"]:
                raise ApiError("engine_failed", f"engine output {name!r} violates media cardinality")
            if not many and isinstance(output, list):
                raise ApiError("engine_failed", f"engine output {name!r} must be scalar")
            if not all(isinstance(value, dict) and value.get("kind") == field["kind"] for value in values):
                raise ApiError("engine_failed", f"engine output {name!r} has wrong kind")
            if field["kind"] == "json":
                value = output.get("value")
                out_schema = field["schema"]
                if out_schema == schemas.DIGEST_ID and isinstance(value, dict):
                    tools = value.get("tools") or {}
                    if any(v in ("failed", "partial") for v in tools.values()) and not job.source["degraded"]:
                        job.source["degraded"] = True
                        job.source["degraded_reason"] = "partial_failure"
                    value["source"] = job.source
                    content_type = next((v for v in job.media_content_types.values() if isinstance(v, str)), None)
                    self.registry.populate_capabilities_available(value, content_type, include_internal=job.bound)
                validator = schemas.validator_for(out_schema)
                if validator is not None:
                    errs = schemas.errors(validator, value)
                    if errs:
                        raise ApiError("engine_failed", f"engine output {name!r} violates schema: " + "; ".join(errs[:3]))
            elif field["kind"] == "text":
                if not isinstance(output.get("text"), str):
                    raise ApiError("engine_failed", f"engine output {name!r} must contain text")
                validator = schemas.validator_for(field["schema"])
                if validator is not None:
                    errs = schemas.errors(validator, output["text"])
                    if errs:
                        raise ApiError("engine_failed", f"engine output {name!r} violates schema: " + "; ".join(errs[:3]))
            elif field["kind"] == "media":
                if any(not any(self.registry._matches_media_type(pattern, value.get("content_type", ""))
                               for pattern in field["produces"]) or
                       not isinstance(value.get("data_base64"), str) for value in values):
                    raise ApiError("engine_failed", f"engine output {name!r} has invalid media payload")
        return outputs

    def _agent_context(self, job: Job, outputs: dict) -> dict | None:
        value = next((output.get("value") for output in outputs.values()
                      if isinstance(output, dict) and output.get("kind") == "json"), None)
        context = self.registry.present_agent_context(job.task_type, value)
        if context is None:
            return None
        v = schemas.validator_for_ref("urn:mmp:protocol:3:job-api#/$defs/AgentContext")
        errs = schemas.errors(v, context)
        if errs:
            raise ApiError("engine_failed", "agent_context violates protocol: " + "; ".join(errs[:3]))
        return context

    async def _place_outputs(self, job: Job, outputs: dict) -> None:
        """把 engine media payload 交付到调用方 PUT 或转换为受限内联值。"""
        public: dict = {}
        for name, output in outputs.items():
            field = next(field for field in self.registry.capability(job.task_type)["output"]["fields"]
                         if field["name"] == name)
            if field["kind"] != "media":
                public[name] = output
                continue
            many = field["cardinality"]["max"] != 1
            values = output if many else [output]
            endpoints = job.delivery.get(name)
            if endpoints is not None and not many:
                endpoints = [endpoints]
            if endpoints is not None and len(endpoints) != len(values):
                raise ApiError("bad_request", f"delivery.{name} must contain one endpoint per media output")
            placed = []
            for index, value in enumerate(values):
                placed.append(await self._place_media(name, value, None if endpoints is None else endpoints[index]))
            public[name] = placed if many else placed[0]
        job.outputs = public

    async def _place_media(self, name: str, output: dict, endpoint: dict | None) -> dict:
        """交付一个 media atom，并剥离只允许存在于 A/cache 内部的 data_base64。"""
        try:
            blob = base64.b64decode(output["data_base64"], validate=True)
        except (binascii.Error, ValueError) as e:
            raise ApiError("engine_failed", f"engine output {name!r} is not valid base64") from e
        media_id = self.media.store(blob, output["content_type"])
        descriptor = {"kind": "media", "content_type": output["content_type"], "bytes": len(blob),
                      "sha256": media_id.removeprefix("sha256:"), "media_id": media_id}
        if endpoint is not None:
            await self.media.put(endpoint, blob, output["content_type"])
            descriptor["delivered"] = True
        else:
            if len(blob) > self.cfg.media.max_inline_result_bytes:
                raise ApiError("engine_failed", f"media output {name!r} is {len(blob)} bytes > inline limit and has no delivery endpoint")
            descriptor["inline"] = output["data_base64"]
        return descriptor

    def _unpin(self, job: Job) -> None:
        for value in job.media_ids.values():
            for media_id in value if isinstance(value, list) else [value]:
                self.media.unpin(media_id)
