"""S4 集成验收：真起 B-ts（node ts/packages/broker/dist/cli.js），A 的 WsLink 连过去。

- echo 经 ws 往返；ws 上的信封过 ab-message schema（B-ts 会校验，非法帧被丢）
- 拔掉 A 的 ws：C 的在途 GET 在 wait+grace 内收到 503 node_offline；A 退避重连、重 register
- 重启 B-ts：A 自动重连，C 重试成功；B 不落盘
- 错密钥：4001，A 不再重试
需要先 `pnpm -r build`（ts/）；dist 不在则跳过。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from conftest import NODE_DIR, V_ANY, assert_valid, blob, inline, make_config
from mmp_node.engine_io import EngineResult
from mmp_node.config import BrokerEndpoint
from mmp_node.node import Node
from mmp_node.ws_client import WsLink

CLI = NODE_DIR.parent / "ts" / "packages" / "broker" / "dist" / "cli.js"
OPENCLAW_SMOKE = Path(__file__).with_name("openclaw_host_smoke.mjs")
CLIENT_CAPABILITY_SMOKE = Path(__file__).with_name("client_capability_smoke.mjs")
NODE_BIN = shutil.which("node")
KEY = "integration-node-key-0123456789"
sys.path.insert(0, str(NODE_DIR))

pytestmark = pytest.mark.skipif(NODE_BIN is None or not CLI.exists(), reason="需要 node 与已构建的 ts/packages/broker（pnpm -r build）")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BrokerProc:
    def __init__(self, port: int, cwd: Path, key: str = KEY, grace: float = 1.0):
        self.port, self.cwd, self.key, self.grace = port, cwd, key, grace
        self.proc: subprocess.Popen | None = None

    async def start(self) -> None:
        env = {**os.environ, "MMP_BROKER__HOST": "127.0.0.1", "MMP_BROKER__PORT": str(self.port), "MMP_BROKER__NODE_KEY": self.key,
               "MMP_BROKER__INFLIGHT_GRACE_SEC": str(self.grace), "MMP_BROKER__REGISTER_TIMEOUT_SEC": "5"}
        self.proc = subprocess.Popen([NODE_BIN, str(CLI)], cwd=self.cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        async with httpx.AsyncClient(trust_env=False) as c:
            for _ in range(100):
                try:
                    r = await c.get(f"http://127.0.0.1:{self.port}/health")
                    if r.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                if self.proc.poll() is not None:
                    raise RuntimeError(f"broker exited: {self.proc.stderr.read().decode(errors='replace')}")
                await asyncio.sleep(0.1)
        raise RuntimeError("broker did not come up")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    async def wait_nodes(self, n: int, timeout: float = 10) -> dict:
        async with httpx.AsyncClient(trust_env=False) as c:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    h = (await c.get(f"http://127.0.0.1:{self.port}/health")).json()
                    if len(h["nodes"]) == n:
                        return h
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
        raise AssertionError(f"broker never showed {n} node(s)")


def endpoint(port: int, key: str = KEY) -> BrokerEndpoint:
    return BrokerEndpoint(url=f"ws://127.0.0.1:{port}/ws", key=key, heartbeat_interval_sec=0.5,
                         backoff_min_sec=0.2, backoff_max_sec=1.0)


async def test_echo_over_ws_unplug_and_restart(tmp_path):
    port = free_port()
    bcwd = tmp_path / "broker-cwd"
    bcwd.mkdir()
    b = BrokerProc(port, bcwd)
    cfg = make_config(tmp_path, broker__enabled=False)
    cfg.brokers = [endpoint(port)]
    node = Node(cfg, NODE_DIR)
    await node.start()
    link = WsLink(node, cfg.brokers[0])
    base = f"http://127.0.0.1:{port}"
    try:
        await b.start()
        link.start()
        h = await b.wait_nodes(1)
        assert h["status"] == "ok" and h["nodes"][0]["node_id"] == "t-node"

        async with httpx.AsyncClient(base_url=base, timeout=30, trust_env=False) as c:
            # ---- echo 往返 ----
            r = await c.post("/jobs", json={"type": "echo", "media": inline(blob(100)), "params": {"tag": "ws"}, "wait": 10})
            assert r.status_code == 200, r.text
            assert_valid(V_ANY, r.json())
            assert r.json()["status"] == "done" and r.json()["result"]["params"]["tag"] == "ws"
            caps = (await c.get("/capabilities")).json()
            assert caps["capabilities"][0]["capability"]["id"] == "echo" and caps["capabilities"][0]["nodes"] == ["t-node"]

            # ---- 拔线：在途 GET(wait=5) 必须在 wait+grace 内得到 503 node_offline ----
            r = await c.post("/jobs", json={"type": "echo", "media": inline(blob(100)), "params": {"sleep_ms": 4000, "tag": "slow"}})
            assert r.status_code == 202
            jid = r.json()["job_id"]
            t0 = time.monotonic()
            pending = asyncio.create_task(c.get(f"/jobs/{jid}", params={"wait": 5}))
            await asyncio.sleep(0.3)
            await link.drop_connection()
            r = await pending
            elapsed = time.monotonic() - t0
            assert r.status_code == 503 and r.json()["error"] == "node_offline", r.text
            assert elapsed < 5 + 1.0 + 1.0, f"took {elapsed:.1f}s"

            # ---- A 自动重连重注册；任务还在 A 上，重试拿到结果 ----
            await b.wait_nodes(1)
            assert link.reconnects >= 1
            r = await c.get(f"/jobs/{jid}", params={"wait": 10})
            assert r.status_code == 200 and r.json()["status"] == "done", r.text

        # ---- 重启 B-ts：A 重连；C 重试成功；B 不落盘 ----
        b.stop()
        await asyncio.sleep(0.5)
        async with httpx.AsyncClient(base_url=base, timeout=5, trust_env=False) as c:
            with pytest.raises(httpx.HTTPError):
                await c.get("/health")
        b2 = BrokerProc(port, bcwd)
        await b2.start()
        try:
            await b2.wait_nodes(1, timeout=15)
            async with httpx.AsyncClient(base_url=base, timeout=30, trust_env=False) as c:
                r = await c.post("/jobs", json={"type": "echo", "media": inline(blob(100)), "params": {"tag": "after-restart"}, "wait": 10})
                assert r.status_code == 200 and r.json()["status"] == "done"
                # 重启前的任务 B 一无所知（无状态），但 A 还记得：GET 仍然路由回去
                r = await c.get(f"/jobs/{jid}")
                assert r.status_code == 200 and r.json()["status"] == "done"
            assert list(bcwd.iterdir()) == [], "B-ts must not write anything to disk"
        finally:
            b2.stop()
    finally:
        b.stop()
        await link.close()
        await node.close()


async def test_openclaw_host_discovers_image_triage_and_injects_context(tmp_path):
    """MMP 自有宿主端到端：OpenClaw 适配 → B-ts HTTP/ws → A → 任务 presenter。"""
    from engines.triage_image.digest import RawRegion, build_digest

    port = free_port()
    broker = BrokerProc(port, tmp_path)
    cfg = make_config(tmp_path, engines={"triage_image": {"module": "engines.triage_image", "timeout_sec": 5}},
                      broker__enabled=False, scheduler__vram_total_mb=1024)
    cfg.brokers = [endpoint(port)]
    node = Node(cfg, NODE_DIR)

    async def fake_run(_spec, job, _timeout, _vram):
        surfaces = [{"id": "image_0", "kind": "image", "index": 0, "width_px": 64,
                     "height_px": 48, "rotation_deg": 0}]
        digest, _ = build_digest(job["media_id"], "image/png", surfaces, ["A person holds a contract."],
                                 [RawRegion("image_0", "contract", [4, 5, 40, 30], "phrase_grounding")],
                                 0, .8, {"caption": 1})
        return EngineResult(digest, {"caption": 1})

    node.pool.run = fake_run
    await node.start()
    link = WsLink(node, cfg.brokers[0])
    try:
        await broker.start()
        link.start()
        await broker.wait_nodes(1)
        proc = await asyncio.create_subprocess_exec(
            NODE_BIN, str(OPENCLAW_SMOKE), f"http://127.0.0.1:{port}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 20)
        assert proc.returncode == 0, stderr.decode(errors="replace")
        assert b'"mediaId":"sha256:' in stdout
    finally:
        broker.stop()
        await link.close()
        await node.close()


async def test_client_discovers_describes_and_runs_image_capability(tmp_path):
    """通用 C 端只从 digest 取能力 id，调用时再 describe，然后沿同一媒体 ref 执行。"""
    from engines.triage_image.digest import RawRegion, build_digest

    port = free_port()
    broker = BrokerProc(port, tmp_path)
    cfg = make_config(tmp_path, engines={
        "triage_image": {"module": "engines.triage_image", "timeout_sec": 5},
        "ocr_structured": {"module": "engines.ocr_structured", "timeout_sec": 5},
    }, broker__enabled=False, scheduler__vram_total_mb=16384)
    cfg.brokers = [endpoint(port)]
    node = Node(cfg, NODE_DIR)

    async def fake_run(spec, job, _timeout, _vram):
        if spec.name == "triage_image":
            surfaces = [{"id": "image_0", "kind": "image", "index": 0, "width_px": 64,
                         "height_px": 48, "rotation_deg": 0}]
            digest, _ = build_digest(
                job["media_id"], "image/png", surfaces, ["A person holds a contract."],
                [RawRegion("image_0", "contract", [4, 5, 40, 30], "phrase_grounding")],
                0, .8, {"caption": 1},
            )
            return EngineResult(digest, {"caption": 1})
        return EngineResult({
            "page_count": 1,
            "pages": [{"page": 1, "tier": "gpu-fast", "width": 64, "height": 48,
                       "text": "contract", "lines": [], "quality": {"lines": 1, "chars": 8},
                       "flags": []}],
            "suggest_upgrade_pages": [],
        }, {"ocr": 1})

    node.pool.run = fake_run
    await node.start()
    link = WsLink(node, cfg.brokers[0])
    try:
        await broker.start()
        link.start()
        await broker.wait_nodes(1)
        command = [NODE_BIN, str(CLIENT_CAPABILITY_SMOKE), f"http://127.0.0.1:{port}"]
        if module := os.environ.get("MMP_CLIENT_COMPAT_MODULE"):
            command.append(module)
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 20)
        assert proc.returncode == 0, stderr.decode(errors="replace")
        assert b'"capability":"ocr.structured"' in stdout
    finally:
        broker.stop()
        await link.close()
        await node.close()


async def test_bad_key_is_fatal_and_not_retried(tmp_path):
    port = free_port()
    b = BrokerProc(port, tmp_path)
    cfg = make_config(tmp_path, broker__enabled=False)
    node = Node(cfg, NODE_DIR)
    await node.start()
    link = WsLink(node, endpoint(port, key="wrong-key-0123456789abc"))
    try:
        await b.start()
        link.start()
        for _ in range(50):
            if link.fatal:
                break
            await asyncio.sleep(0.1)
        assert link.fatal is not None and link.fatal[0] == 4001, link.fatal
        await asyncio.sleep(0.6)
        assert link.reconnects == 0
        async with httpx.AsyncClient(timeout=5, trust_env=False) as c:
            assert (await c.get(f"http://127.0.0.1:{port}/health")).json()["status"] == "no_node"
    finally:
        b.stop()
        await link.close()
        await node.close()


async def test_graceful_node_shutdown_returns_node_offline_for_inflight_request(tmp_path):
    port = free_port()
    b = BrokerProc(port, tmp_path)
    cfg = make_config(tmp_path, broker__enabled=False)
    node = Node(cfg, NODE_DIR)
    await node.start()
    link = WsLink(node, endpoint(port))
    closed = False
    try:
        await b.start()
        link.start()
        await b.wait_nodes(1)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30, trust_env=False) as c:
            pending = asyncio.create_task(c.post("/jobs", json={
                "type": "echo", "media": inline(blob(100)),
                "params": {"sleep_ms": 4000, "tag": "shutdown"}, "wait": 60,
            }))
            for _ in range(100):
                if node.sched.running == 1:
                    break
                await asyncio.sleep(0.05)
            assert node.sched.running == 1, "request never reached the engine"

            await node.close()
            closed = True
            r = await pending
            assert r.status_code == 503, r.text
            assert r.json()["status"] == "failed" and r.json()["error"] == "node_offline", r.text
    finally:
        b.stop()
        await link.close()
        if not closed:
            await node.close()


async def test_broker_rejects_non_register_first_frame(tmp_path):
    """首帧不是 register → 4003；schema 不合法的 register → 4003。用裸 websockets 直连验证。"""
    import json
    import websockets
    port = free_port()
    b = BrokerProc(port, tmp_path)
    await b.start()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws", proxy=None) as ws:
            await ws.send(json.dumps({"type": "heartbeat", "protocol_version": "2.0", "ts": "2026-09-21T00:00:00Z",
                                      "payload": {"node_id": "x", "queue_len": 0, "running": 0, "engines_loaded": []}}))
            with pytest.raises(websockets.exceptions.ConnectionClosed) as e:
                await ws.recv()
            assert e.value.rcvd.code == 4003
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws", proxy=None) as ws:
            await ws.send(json.dumps({"type": "register", "protocol_version": "3.0", "ts": "2026-09-21T00:00:00Z",
                                      "payload": {"node_id": "x", "node_key": KEY, "capabilities": [{"id": "echo", "purpose": "diagnostic",
                                                  "tiers": [{"tier": "cpu", "engine": "e", "engine_version": "1", "cost": "low"}],
                                                  "input": {"media": {"presence": "required", "accepts": ["*/*"]}},
                                                  "output": {"schema": {"type": "object"}, "agent_context": "none"}}], "engine_versions": {}}}))
            with pytest.raises(websockets.exceptions.ConnectionClosed) as e:
                await ws.recv()
            assert e.value.rcvd.code == 4002
    finally:
        b.stop()
