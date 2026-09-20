"""A 节点门面：三个任务操作 + A↔B 消息的 A 侧（register / heartbeat 生产，request → response 分发）。

分发函数与传输无关：S1 由进程内 B-py 调用，S4 由 ws 客户端调用，同一份代码。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from .cache import ResultCache, task_key
from .config import Config
from .engine_io import EnginePool
from .errors import ApiError
from .ids import new_job_id
from .jobs import Job
from .media import MediaStore
from .registry import Registry
from .scheduler import Scheduler

log = logging.getLogger("mmp.node")
ENGINES_DIR = Path(__file__).resolve().parent.parent  # node/，`python -m engines.echo` 的 cwd


class Node:
    def __init__(self, cfg: Config, engines_dir: Path = ENGINES_DIR):
        self.cfg = cfg
        self.node_id = cfg.node.id
        cfg.node.data_dir.mkdir(parents=True, exist_ok=True)
        self.registry = Registry(cfg, engines_dir)
        self._check_vram_budget()
        self.media = MediaStore(cfg.media, cfg.node.data_dir)
        self.cache = ResultCache(cfg.cache_path)
        self.pool = EnginePool(cfg.scheduler.engine_start_timeout_sec, cfg.scheduler.idle_unload_sec, cfg.node.protocol_version)
        self.sched = Scheduler(cfg, self.registry, self.pool, self.cache, self.media)
        self.jobs: dict[str, Job] = {}
        self._seq = 0
        self._param_validators: dict[str, Draft202012Validator] = {}
        self._gc_task: asyncio.Task | None = None

    def _check_vram_budget(self) -> None:
        for cap in self.registry.capabilities():
            for t in cap["tiers"]:
                if t.get("vram_mb", 0) > self.cfg.scheduler.vram_total_mb:
                    raise ValueError(f"{cap['id']}@{t['tier']} needs {t['vram_mb']} MiB VRAM > scheduler.vram_total_mb={self.cfg.scheduler.vram_total_mb}")

    async def start(self) -> None:
        self.sched.start()
        self._gc_task = asyncio.create_task(self._gc_loop(), name="mmp-job-gc")

    async def close(self) -> None:
        if self._gc_task:
            self._gc_task.cancel()
        await self.sched.stop()
        await self.pool.close()
        await self.media.aclose()
        self.cache.close()

    async def _gc_loop(self) -> None:
        while True:
            await asyncio.sleep(min(60.0, self.cfg.jobs.retention_sec / 4))
            cutoff = time.time() - self.cfg.jobs.retention_sec
            for jid, j in list(self.jobs.items()):
                if j.terminal and (j.finished_at or 0) < cutoff:
                    del self.jobs[jid]

    # ---- 任务操作（协议.md §0.1 的五个操作里归 A 的三个）----
    async def submit(self, req: dict, wait: float | None = None) -> tuple[int, dict]:
        task_type = req["type"]
        cap = self.registry.capability(task_type)
        tier = self.registry.resolve_tier(task_type, req.get("tier"))
        params = req.get("params") or {}
        self._validate_params(cap, params)
        priority = req.get("priority", "interactive")
        wait_s = float(req.get("wait", 0) if wait is None else wait)

        self.sched.check_backpressure()          # 先于取媒体：别为了说 429 先下载 100MB
        fetched = await self.media.fetch(req["media"])
        key = task_key(fetched.media_id, task_type, tier["tier"], tier["engine_version"], params)

        hit = self.cache.get(key)
        if hit is not None:
            job = self._new_job(task_type, priority, tier, params, req["media"], fetched.media_id, None, key)
            job.cached, job.source = True, hit["source"]
            job.result, job.result_ref, job.timings_ms = hit.get("result"), hit.get("result_ref"), hit.get("timings_ms") or {}
            job.finish("done")
            self.jobs[job.job_id] = job
            return job.response()

        active = self.sched.find_active(key)     # 同键合并：返回已有 job
        if active is not None:
            await active.wait(wait_s)
            return active.response()

        job = self._new_job(task_type, priority, tier, params, req["media"], fetched.media_id, fetched.path, key)
        self.jobs[job.job_id] = job
        self.sched.enqueue(job)
        await job.wait(wait_s)
        return job.response()

    async def get(self, job_id: str, wait: float = 0) -> tuple[int, dict]:
        job = self._job(job_id)
        await job.wait(wait)
        return job.response()

    async def cancel(self, job_id: str, wait: float = 0) -> tuple[int, dict]:
        job = self._job(job_id)
        if not job.terminal:
            self.sched.cancel(job)
            await job.wait(wait)
        return job.response()

    def _job(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise ApiError("not_found", f"unknown job {job_id}")
        return job

    def _new_job(self, task_type, priority, tier, params, media, media_id, media_path, key) -> Job:
        self._seq += 1
        return Job(job_id=new_job_id(self.node_id), task_type=task_type, priority=priority, tier=tier, params=params,
                   media=media, media_id=media_id, media_path=media_path, key=key, seq=self._seq)

    def _validate_params(self, cap: dict, params: dict) -> None:
        schema = cap.get("input", {}).get("params_schema")
        if schema is None:
            if params:
                raise ApiError("bad_request", f"{cap['id']} accepts no params")
            return
        v = self._param_validators.get(cap["id"])
        if v is None:
            v = self._param_validators[cap["id"]] = Draft202012Validator(schema)
        errs = list(v.iter_errors(params))
        if errs:
            raise ApiError("bad_request", "params: " + "; ".join(e.message for e in errs[:3]))

    # ---- A↔B 消息的 A 侧 ----
    def envelope(self, type_: str, payload: dict) -> dict:
        return {"type": type_, "protocol_version": self.cfg.node.protocol_version,
                "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"), "payload": payload}

    def register_message(self) -> dict:
        return self.envelope("register", {
            "node_id": self.node_id, "node_key": self.cfg.node.key,
            "capabilities": self.registry.capabilities(), "engine_versions": self.registry.engine_versions(),
        })

    def heartbeat_message(self, vram_free_mb: int | None = None) -> dict:
        p = {"node_id": self.node_id, "queue_len": self.sched.queue_len, "running": self.sched.running,
             "engines_loaded": self.pool.loaded()}
        if vram_free_mb is not None:
            p["vram_free_mb"] = vram_free_mb
        elif self.cfg.scheduler.vram_total_mb:
            p["vram_free_mb"] = self.cfg.scheduler.vram_total_mb - self.sched._vram_used
        return self.envelope("heartbeat", p)

    async def dispatch(self, request: dict) -> dict:
        """RequestPayload → ResponsePayload。任何 ApiError 变成 (http_status, ErrorBody)，绝不抛出。"""
        try:
            op = request["op"]
            if op == "submit":
                status, body = await self.submit(request["job"])
            elif op == "get":
                status, body = await self.get(request["job_id"], float(request.get("wait", 0)))
            elif op == "cancel":
                status, body = await self.cancel(request["job_id"], float(request.get("wait", 0)))
            else:
                raise ApiError("bad_request", f"unknown op {op!r}")
        except ApiError as e:
            status, body = e.http_status, e.body()
        except Exception as e:
            log.exception("dispatch failed")
            status, body = 500, ApiError("engine_failed", f"{type(e).__name__}: {e}").body()
        return {"req_id": request["req_id"], "http_status": status, "body": body}
