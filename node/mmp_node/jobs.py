"""任务记录与状态机：queued → running → done | failed | cancelled；queued → cancelled。"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ApiError

PRIORITY_RANK = {"interactive": 0, "batch": 1}
TERMINAL = {"done", "failed", "cancelled"}


@dataclass
class Job:
    job_id: str
    task_type: str
    priority: str
    tier: dict                    # 请求解析出的 TierSpec（可能在回落后被替换）
    params: dict
    media: dict                   # 原始句柄；put 端点从这里取
    media_id: str
    media_path: Path | None
    key: str
    seq: int
    created_at: float = field(default_factory=time.time)
    status: str = "queued"
    started_at: float | None = None
    finished_at: float | None = None
    cached: bool = False
    source: dict | None = None
    result: object = None
    result_ref: dict | None = None
    timings_ms: dict = field(default_factory=dict)
    engine_metrics: dict = field(default_factory=dict)  # A 内部观测，不进入 B/C 响应
    error: ApiError | None = None
    cancel_requested: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    queue_position: int | None = None
    eta_sec: float | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def finish(self, status: str, error: ApiError | None = None) -> None:
        self.status = status
        self.error = error
        self.finished_at = time.time()
        self.done.set()

    async def wait(self, seconds: float) -> None:
        if seconds <= 0 or self.terminal:
            return
        try:
            await asyncio.wait_for(self.done.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    def response(self) -> tuple[int, dict]:
        """(http_status, body)，与协议.md §3.3 一一对应。"""
        base = {"job_id": self.job_id, "type": self.task_type, "media_id": self.media_id}
        if self.status == "done":
            body = {**base, "status": "done", "cached": self.cached, "source": self.source}
            if self.result_ref is not None:
                body["result_ref"] = self.result_ref
            else:
                body["result"] = self.result
            if self.timings_ms:
                body["timings_ms"] = self.timings_ms
            return 200, body
        if self.status == "cancelled":
            return 200, {**base, "status": "cancelled"}
        if self.status == "failed":
            assert self.error is not None
            body = {**base, "status": "failed", "error": self.error.code}
            if self.error.message:
                body["message"] = self.error.message
            if self.error.retry_after_sec is not None:
                body["retry_after_sec"] = self.error.retry_after_sec
            return self.error.http_status, body
        body = {**base, "status": self.status}
        if self.queue_position is not None and self.status == "queued":
            body["queue_position"] = self.queue_position
        if self.eta_sec is not None:
            body["eta_sec"] = self.eta_sec
        if self.cancel_requested:
            body["cancel_requested"] = True
        return 202, body
