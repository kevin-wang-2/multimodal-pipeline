"""Broker 操作层（协议.md §0.1）：submit / get / cancel / capabilities / health，每个返回 (http_status, body)。

传输无关：HTTP 绑定（http.py）与进程内调用都走这里；A 侧的连接抽象是 NodeLink。
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from mmp_node.errors import ApiError
from mmp_node.ids import node_of

log = logging.getLogger("mmp.broker")

SendFn = Callable[[dict], Awaitable[dict]]   # RequestPayload → ResponsePayload（信封由传输层包）


class RegisterRejected(Exception):
    """对应 ws 关闭码 4001 / 4002 / 4003；进程内绑定时直接抛出。"""

    def __init__(self, code: int, reason: str):
        super().__init__(f"{code} {reason}")
        self.code = code
        self.reason = reason


@dataclass
class NodeLink:
    node_id: str
    send: SendFn
    capabilities: list[dict]
    engine_versions: dict[str, str]
    connected_since: float = field(default_factory=time.time)
    last_heartbeat: float | None = None
    heartbeat: dict | None = None

    @property
    def queue_len(self) -> int:
        return (self.heartbeat or {}).get("queue_len", 0)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(timespec="seconds")


class Broker:
    def __init__(self, node_key: str, protocol_version: str = "1.1", inflight_grace_sec: float = 5,
                 heartbeat_interval_sec: float = 10, missed_heartbeats: int = 3, now: Callable[[], float] = time.time):
        self.node_key = node_key
        self.protocol_version = protocol_version
        self.inflight_grace_sec = inflight_grace_sec
        self.offline_after = heartbeat_interval_sec * missed_heartbeats
        self.nodes: dict[str, NodeLink] = {}
        self._req_seq = itertools.count(1)
        self.now = now   # 可注入的时钟：契约场景用它拨快心跳超时

    # ---- A 侧消息进入 ----
    def on_register(self, envelope: dict, send: SendFn) -> NodeLink:
        if envelope.get("type") != "register":
            raise RegisterRejected(4003, "first message must be register")
        if str(envelope.get("protocol_version", "")).split(".")[0] != self.protocol_version.split(".")[0]:
            raise RegisterRejected(4002, f"protocol {envelope.get('protocol_version')} incompatible with {self.protocol_version}")
        p = envelope.get("payload") or {}
        if p.get("node_key") != self.node_key:
            raise RegisterRejected(4001, "bad node_key")
        if not p.get("node_id") or not p.get("capabilities"):
            raise RegisterRejected(4003, "register missing node_id or capabilities")
        link = NodeLink(node_id=p["node_id"], send=send, capabilities=p["capabilities"],
                        engine_versions=p.get("engine_versions") or {}, connected_since=self.now())
        old = self.nodes.get(link.node_id)
        if old is not None:
            log.info("node %s re-registered, replacing old link", link.node_id)
        self.nodes[link.node_id] = link
        return link

    def on_heartbeat(self, envelope: dict) -> None:
        p = envelope.get("payload") or {}
        link = self.nodes.get(p.get("node_id"))
        if link is None:
            return
        link.last_heartbeat = self.now()
        link.heartbeat = p

    def on_disconnect(self, node_id: str) -> None:
        self.nodes.pop(node_id, None)

    def _live(self) -> list[NodeLink]:
        """心跳超过 3 个周期没来的节点视为离线（进程内绑定的节点没有心跳超时问题：它和 B 同生死）。"""
        now = self.now()
        for nid, l in list(self.nodes.items()):
            if l.last_heartbeat is not None and now - l.last_heartbeat > self.offline_after:
                log.warning("node %s missed heartbeats, dropping", nid)
                del self.nodes[nid]
        return list(self.nodes.values())

    # ---- 路由 ----
    def _pick(self, task_type: str) -> NodeLink:
        cands = [l for l in self._live() if any(c["id"] == task_type for c in l.capabilities)]
        if not cands:
            raise ApiError("no_node", f"no online node offers {task_type!r}")
        return min(cands, key=lambda l: l.queue_len)

    def _by_job(self, job_id: str) -> NodeLink:
        nid = node_of(job_id)
        if nid is None:
            raise ApiError("bad_request", f"malformed job_id {job_id!r}")
        link = self.nodes.get(nid)
        if link is None or link not in self._live():
            raise ApiError("node_offline", f"node {nid} is not connected")
        return link

    async def _call(self, link: NodeLink, payload: dict, wait: float) -> tuple[int, dict]:
        payload = {"req_id": f"b{next(self._req_seq)}", **payload}
        try:
            resp = await asyncio.wait_for(link.send(payload), wait + self.inflight_grace_sec)
        except asyncio.TimeoutError:
            log.warning("node %s did not answer %s within %.1fs, dropping link", link.node_id, payload["op"], wait + self.inflight_grace_sec)
            self.on_disconnect(link.node_id)
            raise ApiError("node_offline", f"node {link.node_id} did not respond in time")
        return resp["http_status"], resp["body"]

    # ---- 五个操作 ----
    async def submit(self, job: dict) -> tuple[int, dict]:
        try:
            link = self._pick(job["type"])
            return await self._call(link, {"op": "submit", "job": job}, float(job.get("wait", 0)))
        except ApiError as e:
            return e.http_status, e.body()

    async def get(self, job_id: str, wait: float = 0) -> tuple[int, dict]:
        try:
            link = self._by_job(job_id)
            return await self._call(link, {"op": "get", "job_id": job_id, "wait": int(wait)}, wait)
        except ApiError as e:
            return e.http_status, e.body()

    async def cancel(self, job_id: str, wait: float = 0) -> tuple[int, dict]:
        try:
            link = self._by_job(job_id)
            return await self._call(link, {"op": "cancel", "job_id": job_id, "wait": int(wait)}, wait)
        except ApiError as e:
            return e.http_status, e.body()

    def capabilities(self) -> tuple[int, dict]:
        merged: dict[str, dict] = {}
        for l in self._live():
            for cap in l.capabilities:
                entry = merged.setdefault(cap["id"], {"capability": cap, "nodes": []})
                entry["nodes"].append(l.node_id)
        return 200, {"protocol_version": self.protocol_version, "capabilities": list(merged.values())}

    def health(self) -> tuple[int, dict]:
        live = self._live()
        nodes = []
        for l in live:
            n = {"node_id": l.node_id, "connected_since": _iso(l.connected_since),
                 "last_heartbeat": _iso(l.last_heartbeat or l.connected_since)}
            if l.heartbeat:
                n["heartbeat"] = l.heartbeat
            nodes.append(n)
        return 200, {"status": "ok" if live else "no_node", "protocol_version": self.protocol_version, "nodes": nodes}
