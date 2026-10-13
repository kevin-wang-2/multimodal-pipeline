import asyncio
import copy
import sys
import time
import types
from pathlib import Path

import httpx
import pytest

from conftest import NODE_DIR, NODE_KEY, build_stack, inline
from mmp_broker.core import Broker
from mmp_node.engine_io import EngineResult
from mmp_node.ws_client import WsLink


@pytest.fixture
async def provider(tmp_path, monkeypatch):
    sys.path.insert(0, str(NODE_DIR))
    from engines.echo import CAPABILITIES
    cap = copy.deepcopy(CAPABILITIES[0])
    cap.update(id="echo.internal", visibility="internal", purpose="inspect")
    cap["tiers"][0]["engine"] = "private-echo"
    module = types.ModuleType("internal_echo_test")
    module.CAPABILITIES = [cap]
    monkeypatch.setitem(sys.modules, "internal_echo_test", module)
    stack = await build_stack(tmp_path, engines={
        "echo": {"module": "engines.echo", "timeout_sec": 5},
        "private_echo": {"module": "internal_echo_test", "timeout_sec": 5},
    })
    calls = []

    async def run(spec, job, timeout, vram):
        calls.append(spec.name)
        params = job["params"]
        await asyncio.sleep(params.get("sleep_ms", 0) / 1000)
        return EngineResult({"media_id": job["media_id"], "bytes": Path(job["media_path"]).stat().st_size,
                             "params": params, "started_at_ms": int(time.time() * 1000)}, {})

    stack.node.pool.run = run
    yield stack, calls
    await stack.client.aclose()
    await stack.link.close()
    await stack.node.close()


async def test_bound_discovery_and_execution_but_remote_registration_hides_internal(provider):
    stack, calls = provider
    discovered = (await stack.client.get("/capabilities")).json()["capabilities"]
    assert {x["capability"]["id"] for x in discovered} == {"echo", "echo.internal"}
    registration = stack.node.register_message()["payload"]
    assert [cap["id"] for cap in registration["capabilities"]] == ["echo"]
    assert registration["engine_versions"] == {"echo": "1"}
    remote = Broker(NODE_KEY)
    async def send(payload):
        return await stack.node.dispatch(payload)
    # 远端 B 即使收到错误发布的完整表，也不会公开 internal 能力。
    remote.on_register(stack.node.register_message(bound=True), send)
    assert [x["capability"]["id"] for x in remote.capabilities()[1]["capabilities"]] == ["echo"]
    assert "private-echo" not in remote.nodes[stack.node.node_id].engine_versions
    assert (await remote.submit({"type": "echo.internal", "media": inline(b"private"), "wait": 1}))[1]["error"] == "no_node"
    done = (await stack.submit(type="echo.internal", media=inline(b"private"), wait=1)).json()
    assert done["status"] == "done" and calls == ["private_echo"]
    assert (await stack.get(done["job_id"])).json()["status"] == "done"


async def test_remote_forgery_and_internal_job_get_cancel_are_rejected(provider):
    stack, calls = provider
    raw = {"req_id": "forged", "op": "submit", "bound": True,
           "job": {"type": "echo.internal", "media": inline(b"private"), "bound": True}}
    denied = await stack.node.dispatch(raw)
    assert denied["body"]["error"] == "unsupported_type" and calls == []
    pending = (await stack.submit(type="echo.internal", media=inline(b"private"), params={"sleep_ms": 500})).json()
    for op in ("get", "cancel"):
        denied = await stack.node.dispatch({"req_id": op, "op": op, "job_id": pending["job_id"]})
        assert denied["http_status"] == 404 and denied["body"]["error"] == "not_found"
    assert stack.node.jobs[pending["job_id"]].status in {"queued", "running"}
    cancelled = (await stack.cancel(pending["job_id"], wait=1)).json()
    assert cancelled["status"] == "cancelled"


async def test_heartbeat_hides_internal_engine_and_cache_keys_do_not_cross_channels(provider):
    stack, _ = provider
    stack.node.pool.loaded = lambda: ["echo", "private_echo"]
    assert stack.node.heartbeat_message()["payload"]["engines_loaded"] == ["echo"]
    assert stack.node.heartbeat_message(bound=True)["payload"]["engines_loaded"] == ["echo", "private_echo"]
    request = {"type": "echo", "media": inline(b"same"), "wait": 1}
    bound = (await stack.submit(**request)).json()
    _, remote = await stack.node.submit(request)
    assert not remote["cached"]
    assert stack.node.jobs[bound["job_id"]].key != stack.node.jobs[remote["job_id"]].key


async def test_remote_public_job_read_filters_internal_discovery_without_mutating_bound_result(provider):
    stack, _ = provider
    done = (await stack.submit(type="echo", media=inline(b"same"), wait=1)).json()
    job = stack.node.jobs[done["job_id"]]
    job.result = {"kind": "image", "format": "image/png", "capabilities_available": ["echo.internal"]}
    _, remote = await stack.node.get(job.job_id)
    assert remote["result"]["capabilities_available"] == []
    assert job.result["capabilities_available"] == ["echo.internal"]
    assert stack.node.registry.capabilities_for_digest(job.result, include_internal=True) == ["echo.internal"]


async def test_real_remote_broker_cannot_read_or_cancel_bound_internal_job(provider, tmp_path):
    from test_ws_integration import BrokerProc, endpoint, free_port
    stack, _ = provider
    port = free_port()
    broker = BrokerProc(port, tmp_path)
    link = WsLink(stack.node, endpoint(port))
    await broker.start()
    link.start()
    try:
        await broker.wait_nodes(1)
        done = (await stack.submit(type="echo.internal", media=inline(b"private"), wait=1)).json()
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False) as client:
            caps = (await client.get("/capabilities")).json()["capabilities"]
            assert {x["capability"]["id"] for x in caps} == {"echo"}
            denied = await client.post("/jobs", json={"type": "echo.internal", "media": inline(b"private")})
            assert denied.status_code == 503 and denied.json()["error"] == "no_node"
            assert (await client.get(f"/jobs/{done['job_id']}")).status_code == 404
            assert (await client.delete(f"/jobs/{done['job_id']}")).status_code == 404
            public = await client.post("/jobs", json={"type": "echo", "media": inline(b"public"), "wait": 1})
            assert public.json()["status"] == "done"
    finally:
        await link.close()
        broker.stop()
