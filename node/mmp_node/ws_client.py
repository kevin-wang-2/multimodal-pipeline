"""A → B 的出站 ws 连接（协议.md §2.3）。

一个 B 一个 WsLink：连上就 register，每 10s 心跳，收 request 就 dispatch 回 response；断了指数退避重连。
分发函数与进程内绑定共用（Node.dispatch），信封相同。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time

import websockets
from websockets.exceptions import ConnectionClosed

from .config import BrokerEndpoint
from .node import Node

log = logging.getLogger("mmp.ws")
FATAL_CLOSE_CODES = {4001: "unauthorized", 4002: "protocol_mismatch"}   # 不重试，等配置 / 版本变更


class WsLink:
    def __init__(self, node: Node, ep: BrokerEndpoint):
        self.node = node
        self.ep = ep
        self.key = ep.key or node.cfg.node.key
        self._task: asyncio.Task | None = None
        self._ws = None
        self._stop = asyncio.Event()
        self.fatal: tuple[int, str] | None = None     # (关闭码, 原因)；置上后不再重连
        self.connected_since: float | None = None
        self.reconnects = 0

    @property
    def connected(self) -> bool:
        return self._ws is not None and self.connected_since is not None

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name=f"mmp-ws-{self.ep.url}")

    async def close(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def drop_connection(self) -> None:
        """测试 / 运维用：模拟拔线。会触发正常的退避重连。"""
        if self._ws is not None:
            await self._ws.close(code=1001, reason="dropped")

    async def run(self) -> None:
        backoff = self.ep.backoff_min_sec
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.ep.url, max_size=32 * 1024 * 1024, ping_interval=20, ping_timeout=20,
                                              open_timeout=15, proxy=True if self.ep.use_env_proxy else None) as ws:
                    self._ws = ws
                    await ws.send(json.dumps(self.node.register_message(self.key), ensure_ascii=False))
                    self.connected_since = time.time()
                    log.info("connected to %s, registered as %s", self.ep.url, self.node.node_id)
                    hb = asyncio.create_task(self._heartbeat_loop(ws))
                    try:
                        await self._recv_loop(ws)
                    finally:
                        hb.cancel()
                    backoff = self.ep.backoff_min_sec   # 连接曾正常工作过：下次从头退避
            except ConnectionClosed as e:
                if e.rcvd is not None and e.rcvd.code in FATAL_CLOSE_CODES:
                    self.fatal = (e.rcvd.code, e.rcvd.reason or FATAL_CLOSE_CODES[e.rcvd.code])
                    log.error("%s rejected us: %s %s — not retrying", self.ep.url, *self.fatal)
                    break
                log.warning("%s closed: %s", self.ep.url, e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("%s connect failed: %s: %s", self.ep.url, type(e).__name__, e)
            finally:
                self._ws = None
                self.connected_since = None
            if self._stop.is_set():
                break
            delay = backoff * (1 + random.uniform(-self.ep.backoff_jitter, self.ep.backoff_jitter))
            log.info("reconnecting to %s in %.1fs", self.ep.url, delay)
            self.reconnects += 1
            try:
                await asyncio.wait_for(self._stop.wait(), delay)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self.ep.backoff_max_sec)

    async def _heartbeat_loop(self, ws) -> None:
        while True:
            await ws.send(json.dumps(self.node.heartbeat_message(), ensure_ascii=False))
            await asyncio.sleep(self.ep.heartbeat_interval_sec)

    async def _recv_loop(self, ws) -> None:
        inflight: set[asyncio.Task] = set()
        try:
            async for raw in ws:
                try:
                    env = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("%s: non-json frame ignored", self.ep.url)
                    continue
                if env.get("type") != "request":
                    log.warning("%s: unexpected message type %r ignored", self.ep.url, env.get("type"))
                    continue
                t = asyncio.create_task(self._handle(ws, env["payload"]))
                inflight.add(t)
                t.add_done_callback(inflight.discard)
        finally:
            for t in inflight:
                t.cancel()

    async def _handle(self, ws, request: dict) -> None:
        resp = await self.node.dispatch(request)
        try:
            await ws.send(json.dumps(self.node.envelope("response", resp), ensure_ascii=False))
        except ConnectionClosed:
            log.warning("%s: connection closed before response %s could be sent", self.ep.url, request.get("req_id"))
