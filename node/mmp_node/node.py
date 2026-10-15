"""A 节点门面：三个任务操作 + A↔B 消息的 A 侧（register / heartbeat 生产，request → response 分发）。

分发函数与传输无关：S1 由进程内 B-py 调用，S4 由 ws 客户端调用，同一份代码。
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from . import PROTOCOL_VERSION, schemas
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
        self.pool = EnginePool(cfg.scheduler.engine_start_timeout_sec, cfg.scheduler.idle_unload_sec,
                               PROTOCOL_VERSION, self.registry.engines.values(),
                               cfg.scheduler.vram_total_mb, cfg.scheduler.vram_headroom_mb)
        self.sched = Scheduler(cfg, self.registry, self.pool, self.cache, self.media)
        self.jobs: dict[str, Job] = {}
        self._preparing: dict[str, Job] = {}
        self._seq = 0
        self._field_validators: dict[tuple[str, str], Draft202012Validator] = {}
        self._gc_task: asyncio.Task | None = None

    def _check_vram_budget(self) -> None:
        usable = self.cfg.scheduler.vram_total_mb - self.cfg.scheduler.vram_headroom_mb
        if usable < 0:
            raise ValueError("scheduler.vram_headroom_mb exceeds vram_total_mb")
        for cap in self.registry.capabilities(include_internal=True):
            for t in cap["tiers"]:
                if t.get("vram_mb", 0) > usable:
                    raise ValueError(f"{cap['id']}@{t['tier']} needs {t['vram_mb']} MiB VRAM > usable VRAM={usable}")
        for spec in self.registry.engines.values():
            if spec.run_vram_mb > usable:
                raise ValueError(f"internal engine {spec.name} needs {spec.run_vram_mb} MiB VRAM > usable VRAM={usable}")
            if spec.keep_warm and spec.resident_vram_mb <= 0:
                raise ValueError(f"keep_warm engine {spec.name} must declare resident_vram_mb")

    async def start(self) -> None:
        self.pool.start()
        self.sched.start()
        self._gc_task = asyncio.create_task(self._gc_loop(), name="mmp-job-gc")

    def begin_shutdown(self) -> None:
        self.sched.begin_shutdown()
        for job in self._preparing.values():
            if job.task:
                job.task.cancel()

    async def close(self) -> None:
        self.begin_shutdown()
        if self._gc_task:
            self._gc_task.cancel()
        tasks = [job.task for job in self._preparing.values() if job.task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
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
    async def submit(self, req: dict, wait: float | None = None, *, bound: bool = False) -> tuple[int, dict]:
        task_type = req["type"]
        cap = self.registry.capability(task_type)
        if cap["visibility"] == "internal" and not bound:
            raise ApiError("unsupported_type", f"unknown task type {task_type!r}")
        tier = self.registry.resolve_tier(task_type, req.get("tier"))
        priority = req.get("priority", "interactive")
        wait_s = float(req.get("wait", 0) if wait is None else wait)

        self.sched.check_backpressure(len(self._preparing))
        delivery = self._validate_delivery(cap, req.get("delivery") or {})
        arguments = self._validate_arguments(cap, req.get("arguments") or {})
        submission_key = task_key(task_type, tier["tier"], tier["engine_version"], arguments, bound=bound)
        job = next((j for j in self.jobs.values() if not j.terminal
                    and j.submission_key == submission_key and j.delivery == delivery), None)
        if job is None:
            job = self._new_job(task_type, priority, tier, arguments, {}, {}, delivery, "", bound)
            job.submission_key = submission_key
            self.jobs[job.job_id] = job
            self._preparing[job.job_id] = job
            job.task = asyncio.create_task(self._prepare_job(job, cap), name=f"prepare-{job.job_id}")
            # 让纯本地准备有机会入队；网络下载不会阻塞 submit 的 wait 预算。
            await asyncio.sleep(0)
        await job.wait(wait_s)
        return self._response(job, bound=bound)

    async def _prepare_job(self, job: Job, cap: dict) -> None:
        enqueued = False
        try:
            t_fetch = time.monotonic()
            arguments, media_ids, media_types, cache_arguments = await self._prepare_arguments(cap, job.arguments, job)
            job.arguments, job.media_ids, job.media_content_types = arguments, media_ids, media_types
            job.timings_ms["fetch"] = int((time.monotonic() - t_fetch) * 1000)
            job.key = task_key(job.task_type, job.tier["tier"], job.tier["engine_version"], cache_arguments, bound=job.bound)
            hit = self.cache.get(job.key)
            if hit is not None:
                job.cached, job.source = True, hit["source"]
                job.usage = {"served_from": "cache", "tier": job.source["tier"], "engine": job.source["engine"],
                             "compute_ms": 0, "wasted_ms": 0}
                job.raw_outputs = self.sched._finish_outputs(job, hit["outputs"])
                job.agent_context = self.sched._agent_context(job, job.raw_outputs)
                await self.sched._place_outputs(job, job.raw_outputs)
                job.finish("done")
            else:
                active = self.sched.find_active(job.key, job.delivery)
                if active is not None:
                    # 不同媒体句柄解析为同一内容时共享计算，保留已返回的任务身份。
                    await active.done.wait()
                    for name in ("source", "outputs", "raw_outputs", "agent_context", "usage"):
                        setattr(job, name, copy.deepcopy(getattr(active, name)))
                    job.timings_ms.update({k: v for k, v in active.timings_ms.items() if k != "fetch"})
                    job.finish(active.status, active.error)
                else:
                    self.sched.enqueue(job)
                    enqueued = True
        except asyncio.CancelledError:
            if self.sched._stopping:
                job.finish("failed", ApiError("node_offline", "node is shutting down"))
            else:
                job.finish("cancelled")
        except ApiError as e:
            job.finish("failed", e)
        except Exception as e:
            log.exception("job %s failed preparing media", job.job_id)
            job.finish("failed", ApiError("engine_failed", f"{type(e).__name__}: {e}"))
        finally:
            self._preparing.pop(job.job_id, None)
            if not enqueued:
                for media_id in self._all_media_ids(job):
                    self.media.unpin(media_id)
                log.info(json.dumps({"job": job.job_id, "type": job.task_type, "status": job.status,
                                     "cached": job.cached, "usage": job.usage, "timings_ms": job.timings_ms},
                                    ensure_ascii=False))

    async def get(self, job_id: str, wait: float = 0, *, bound: bool = False) -> tuple[int, dict]:
        job = self._job(job_id, bound=bound)
        await job.wait(wait)
        return self._response(job, bound=bound)

    async def cancel(self, job_id: str, wait: float = 0, *, bound: bool = False) -> tuple[int, dict]:
        job = self._job(job_id, bound=bound)
        if not job.terminal:
            if job.job_id in self._preparing:
                job.cancel_requested = True
                job.task.cancel()
                await asyncio.gather(job.task, return_exceptions=True)
            else:
                self.sched.cancel(job)
            await job.wait(wait)
        return self._response(job, bound=bound)

    def _response(self, job: Job, *, bound: bool) -> tuple[int, dict]:
        status, body = job.response()
        if job.status == "done" and job.bound and not bound:
            body = copy.deepcopy(body)
            for output in body.get("outputs", {}).values():
                if isinstance(output, dict) and output.get("kind") == "json":
                    self.registry.populate_capabilities_available(output.get("value"), include_internal=False)
        return status, body

    def _job(self, job_id: str, *, bound: bool) -> Job:
        job = self.jobs.get(job_id)
        if job is None or (not bound and self.registry.capability(job.task_type)["visibility"] == "internal"):
            raise ApiError("not_found", f"unknown job {job_id}")
        return job

    def _new_job(self, task_type, priority, tier, arguments, media_ids, media_types, delivery, key, bound) -> Job:
        self._seq += 1
        return Job(job_id=new_job_id(self.node_id), task_type=task_type, priority=priority, tier=tier,
                   arguments=arguments, media_ids=media_ids, media_content_types=media_types, delivery=delivery,
                   key=key, seq=self._seq, bound=bound)

    @staticmethod
    def _normalize_json(schema: dict, value: dict) -> dict:
        out = dict(value)
        for name, prop in (schema.get("properties") or {}).items():
            if name not in out and isinstance(prop, dict) and "default" in prop:
                out[name] = prop["default"]
        return out

    def _validate_arguments(self, cap: dict, raw: dict) -> dict:
        fields = {field["name"]: field for field in cap["input"]["fields"]}
        if extra := set(raw) - set(fields):
            raise ApiError("bad_request", f"unknown arguments {sorted(extra)}")
        arguments = {}
        for name, field in fields.items():
            if field["required"] and name not in raw:
                raise ApiError("bad_request", f"missing required argument {name!r}")
        # 先验证不需要 I/O 的字段；错误 prompt/options 不应触发媒体下载。
        for name, field in fields.items():
            if field["kind"] == "media":
                continue
            if name not in raw:
                if field["kind"] == "json":
                    value = self._normalize_json(field["schema"], {})
                    if value:
                        arguments[name] = value
                continue
            value = raw[name]
            if field["kind"] == "text" and not isinstance(value, str):
                raise ApiError("bad_request", f"arguments.{name} must be text")
            if field["kind"] == "json" and isinstance(value, dict):
                value = self._normalize_json(field["schema"], value)
            validator = self._field_validators.setdefault((cap["id"], name), Draft202012Validator(field["schema"]))
            errs = list(validator.iter_errors(value))
            if errs:
                raise ApiError("bad_request", f"arguments.{name}: " + "; ".join(e.message for e in errs[:3]))
            arguments[name] = value
        for name, field in fields.items():
            if field["kind"] != "media" or name not in raw:
                continue
            value = raw[name]
            many = field["cardinality"]["max"] != 1
            handles = value if many and isinstance(value, list) else [value]
            if not isinstance(handles, list) or not all(isinstance(item, dict) for item in handles):
                raise ApiError("bad_request", f"arguments.{name} must be a media handle{' array' if many else ''}")
            if not field["cardinality"]["min"] <= len(handles) <= field["cardinality"]["max"]:
                raise ApiError("bad_request", f"arguments.{name} violates media cardinality")
            handle_validator = schemas.validator_for_ref("urn:mmp:protocol:3:media-handle")
            for handle in handles:
                errs = schemas.errors(handle_validator, handle)
                if errs:
                    raise ApiError("bad_request", f"arguments.{name}: " + "; ".join(errs[:3]))
            arguments[name] = copy.deepcopy(value)
        return arguments

    async def _prepare_arguments(self, cap: dict, arguments: dict, job: Job) -> tuple[dict, dict, dict, dict]:
        arguments = copy.deepcopy(arguments)
        media_ids, media_types, cache_arguments = {}, {}, dict(arguments)
        for field in cap["input"]["fields"]:
            name = field["name"]
            if field["kind"] != "media" or name not in arguments:
                continue
            many = field["cardinality"]["max"] != 1
            handles = arguments[name] if many and isinstance(arguments[name], list) else [arguments[name]]
            fetched = []
            for handle in handles:
                item = await self.media.fetch(handle)
                self.media.pin(item.media_id)
                fetched.append(item)
                # 逐个记录 pin：后续字段下载失败或取消时也能释放已准备的媒体。
                ids = [item.media_id for item in fetched]
                job.media_ids[name] = ids if many else ids[0]
            for item in fetched:
                self._validate_media_type(cap["id"], field, item.content_type)
            resolved = [{"media_id": item.media_id, "path": str(item.path),
                         **({"content_type": item.content_type} if item.content_type else {})} for item in fetched]
            cached = [{"media_id": item.media_id, **({"content_type": item.content_type} if item.content_type else {})}
                      for item in fetched]
            ids = [item.media_id for item in fetched]
            types = [item.content_type for item in fetched]
            arguments[name] = resolved if many else resolved[0]
            cache_arguments[name] = cached if many else cached[0]
            media_ids[name] = ids if many else ids[0]
            media_types[name] = types if many else types[0]
        return arguments, media_ids, media_types, cache_arguments

    @staticmethod
    def _validate_delivery(cap: dict, delivery: dict) -> dict:
        media_outputs = {field["name"]: field for field in cap["output"]["fields"] if field["kind"] == "media"}
        if extra := set(delivery) - set(media_outputs):
            raise ApiError("bad_request", f"delivery names unknown media outputs {sorted(extra)}")
        for name, endpoint in delivery.items():
            many = media_outputs[name]["cardinality"]["max"] != 1
            if many != isinstance(endpoint, list):
                raise ApiError("bad_request", f"delivery.{name} must be {'an endpoint array' if many else 'one endpoint'}")
        return delivery

    @staticmethod
    def _all_media_ids(job: Job) -> list[str]:
        return [item for value in job.media_ids.values() for item in (value if isinstance(value, list) else [value])]

    @staticmethod
    def _validate_media_type(capability_id: str, field: dict, content_type: str | None) -> None:
        if content_type is None:
            return
        actual = content_type.split(";", 1)[0].strip().lower()
        accepts = field["accepts"]
        matched = any(pattern == "*/*" or pattern == actual or
                      (pattern.endswith("/*") and actual.startswith(pattern[:-1]))
                      for pattern in accepts)
        if not matched:
            raise ApiError("bad_request", f"{capability_id}.{field['name']} does not accept content_type {actual!r}")

    # ---- A↔B 消息的 A 侧 ----
    def envelope(self, type_: str, payload: dict) -> dict:
        return {"type": type_, "protocol_version": PROTOCOL_VERSION,
                "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"), "payload": payload}

    def register_message(self, key: str | None = None, *, bound: bool = False) -> dict:
        return self.envelope("register", {
            "node_id": self.node_id, "node_key": key or self.cfg.node.key,
            "capabilities": self.registry.capabilities(include_internal=bound),
            "engine_versions": self.registry.engine_versions(include_internal=bound),
        })

    def heartbeat_message(self, vram_free_mb: int | None = None, *, bound: bool = False) -> dict:
        p = {"node_id": self.node_id, "queue_len": self.sched.queue_len + len(self._preparing), "running": self.sched.running,
             "engines_loaded": [name for name in self.pool.loaded() if bound or any(
                 cap["visibility"] == "public" for cap in self.registry.engines[name].capabilities)]}
        if vram_free_mb is not None:
            p["vram_free_mb"] = vram_free_mb
        elif self.cfg.scheduler.vram_total_mb:
            p["vram_free_mb"] = self.cfg.scheduler.vram_total_mb - self.sched._vram_used
        return self.envelope("heartbeat", p)

    async def dispatch(self, request: dict, *, bound: bool = False) -> dict:
        """RequestPayload → ResponsePayload。任何 ApiError 变成 (http_status, ErrorBody)，绝不抛出。"""
        try:
            op = request["op"]
            if op == "submit":
                status, body = await self.submit(request["job"], bound=bound)
            elif op == "get":
                status, body = await self.get(request["job_id"], float(request.get("wait", 0)), bound=bound)
            elif op == "cancel":
                status, body = await self.cancel(request["job_id"], float(request.get("wait", 0)), bound=bound)
            else:
                raise ApiError("bad_request", f"unknown op {op!r}")
        except ApiError as e:
            status, body = e.http_status, e.body()
        except Exception as e:
            log.exception("dispatch failed")
            status, body = 500, ApiError("engine_failed", f"{type(e).__name__}: {e}").body()
        return {"req_id": request["req_id"], "http_status": status, "body": body}
