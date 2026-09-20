"""A/B 绑定：Node 与 Broker 在同一进程。仍走完整的信封与分发函数，只是没有 ws。"""
from __future__ import annotations

import asyncio
import logging

from mmp_node.node import Node

from .core import Broker

log = logging.getLogger("mmp.inproc")


class InProcLink:
    def __init__(self, node: Node, broker: Broker, heartbeat_interval_sec: float = 10):
        self.node = node
        self.broker = broker
        self.interval = heartbeat_interval_sec
        self._hb: asyncio.Task | None = None

    async def _send(self, request_payload: dict) -> dict:
        # B → A 的 request 信封；A 回 response 信封。与 ws 上传的是同一份 JSON。
        env = self.node.envelope("request", request_payload)
        resp_env = self.node.envelope("response", await self.node.dispatch(env["payload"]))
        return resp_env["payload"]

    async def connect(self) -> None:
        # 抛 RegisterRejected 即启动失败（对应 ws 关闭码）
        self.broker.on_register(self.node.register_message(), self._send)
        self.broker.on_heartbeat(self.node.heartbeat_message())
        self._hb = asyncio.create_task(self._heartbeat_loop(), name="mmp-inproc-heartbeat")

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            self.broker.on_heartbeat(self.node.heartbeat_message())

    async def close(self) -> None:
        if self._hb:
            self._hb.cancel()
        self.broker.on_disconnect(self.node.node_id)
