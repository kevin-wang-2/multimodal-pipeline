"""提交等待上限覆盖媒体准备；已接受的下载可查询、取消并受背压约束。"""
import asyncio

import pytest

from conftest import arguments, blob, build_stack, inline, output
from mmp_node.errors import ApiError


@pytest.fixture
async def slow_fetch(stack, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = stack.node.media.fetch

    async def fetch(handle):
        entered.set()
        await release.wait()
        return await original(inline(blob()))

    monkeypatch.setattr(stack.node.media, "fetch", fetch)
    return entered, release


async def test_submit_returns_job_before_download_and_survives_broker_deadline(stack, slow_fetch):
    entered, release = slow_fetch
    stack.broker.inflight_grace_sec = 0.05
    request = {"type": "echo", "arguments": arguments({"get": {"url": "https://media.test/slow"}})}
    first = await asyncio.wait_for(stack.submit(**request), 0.5)
    await entered.wait()
    assert first.status_code == 202
    job_id = first.json()["job_id"]
    assert first.json()["media_ids"] == {}
    # 让下载明确跨过 broker 提交期限；任务仍可查询、同请求不会重复下载。
    await asyncio.sleep(0.1)
    assert (await stack.get(job_id)).json()["status"] == "queued"
    assert (await stack.submit(**request)).json()["job_id"] == job_id
    assert stack.node.heartbeat_message()["payload"]["queue_len"] == 1
    release.set()
    done = await stack.finish(job_id)
    assert done["status"] == "done" and done["media_ids"]["media"]
    assert output(done)["bytes"] == len(blob())
    assert stack.node.media._pins == {}


async def test_wait_budget_includes_download(stack, slow_fetch):
    entered, release = slow_fetch
    # 请求长等也必须先按 wait 返回 202，不能等到下载后再开始算 wait。
    status, body = await asyncio.wait_for(stack.node.submit({
        "type": "echo", "arguments": arguments(inline(blob())), "wait": 0.02,
    }), 0.5)
    assert entered.is_set() and not release.is_set()
    assert status == 202
    job_id = body["job_id"]
    release.set()
    assert (await stack.finish(job_id))["status"] == "done"


async def test_download_failure_is_queryable(stack, monkeypatch):
    release = asyncio.Event()

    async def fetch(_handle):
        await release.wait()
        raise ApiError("media_fetch_failed", "download failed")

    monkeypatch.setattr(stack.node.media, "fetch", fetch)
    first = await stack.submit(type="echo", arguments=arguments(inline(blob())))
    job_id = first.json()["job_id"]
    release.set()
    failed = await stack.get(job_id, wait=1)
    assert failed.status_code == 422
    assert failed.json()["job_id"] == job_id
    assert failed.json()["status"] == "failed"
    assert failed.json()["error"] == "media_fetch_failed"
    assert stack.node.media._pins == {}


async def test_cancel_download_releases_queue_and_prevents_execution(stack, slow_fetch):
    entered, release = slow_fetch
    stack.node.cfg.queue.max_len = 1
    first = await stack.submit(type="echo", arguments=arguments(inline(blob())))
    await entered.wait()
    blocked = await stack.submit(type="echo", arguments=arguments(inline(blob(seed=1))))
    assert blocked.status_code == 429
    job_id = first.json()["job_id"]
    cancelled = await stack.cancel(job_id)
    assert cancelled.json()["status"] == "cancelled"
    assert not stack.node._preparing and not stack.node.sched.running
    assert stack.node.heartbeat_message()["payload"]["queue_len"] == 0
    assert stack.node.pool.loaded() == []
    release.set()
    assert (await stack.get(job_id)).json()["status"] == "cancelled"
    assert (await stack.submit(type="echo", arguments=arguments(inline(blob(seed=1))), wait=1)).status_code == 200


async def test_shutdown_finishes_preparing_job_as_node_offline(stack, slow_fetch):
    entered, _ = slow_fetch
    first = await stack.submit(type="echo", arguments=arguments(inline(blob())))
    await entered.wait()
    stack.node.begin_shutdown()
    await asyncio.gather(*[j.task for j in stack.node._preparing.values()])
    failed = await stack.get(first.json()["job_id"])
    assert failed.status_code == 503 and failed.json()["error"] == "node_offline"
    assert not stack.node._preparing and stack.node.media._pins == {}


async def test_different_handles_share_compute_without_changing_accepted_ids(stack, monkeypatch):
    from mmp_node.engine_io import EngineResult
    release = asyncio.Event()
    calls = []

    async def run(_spec, request, _timeout, _vram):
        calls.append(request["job_id"])
        await release.wait()
        return EngineResult({"result": {"kind": "json", "value": {
            "media_id": request["arguments"]["media"]["media_id"], "bytes": len(blob()),
            "options": {}, "started_at_ms": 1,
        }}}, {})

    monkeypatch.setattr(stack.node.pool, "run", run)
    first = await stack.submit(type="echo", arguments=arguments(inline(blob())))
    job_id = first.json()["job_id"]
    # 等到计算开始，第二个不同句柄以相同内容寻址。
    for _ in range(20):
        if calls:
            break
        await asyncio.sleep(0)
    mid = stack.node.jobs[job_id].media_ids["media"]
    second = await stack.submit(type="echo", arguments=arguments({"ref": mid}))
    second_id = second.json()["job_id"]
    assert second_id != job_id
    release.set()
    assert output(await stack.finish(job_id)) == output(await stack.finish(second_id))
    assert calls == [job_id]
    assert stack.node.media._pins == {}


@pytest.mark.parametrize("cancel", [False, True])
async def test_partial_media_preparation_releases_pins(stack, monkeypatch, cancel):
    cap = stack.node.registry.capability("echo")
    monkeypatch.setitem(cap["input"]["fields"][0]["cardinality"], "max", 2)
    entered, release = asyncio.Event(), asyncio.Event()
    original = stack.node.media.fetch
    calls = 0

    async def fetch(handle):
        nonlocal calls
        calls += 1
        if calls == 1:
            return await original(handle)
        entered.set()
        await release.wait()
        raise ApiError("media_fetch_failed", "second download failed")

    monkeypatch.setattr(stack.node.media, "fetch", fetch)
    first = await stack.submit(type="echo", arguments={"media": [inline(blob()), inline(blob(seed=1))]})
    await entered.wait()
    assert stack.node.media._pins
    job_id = first.json()["job_id"]
    if cancel:
        assert (await stack.cancel(job_id)).json()["status"] == "cancelled"
    else:
        release.set()
        assert (await stack.finish(job_id))["error"] == "media_fetch_failed"
    assert stack.node.media._pins == {}


async def test_disconnected_submit_waiter_does_not_abandon_accepted_job(stack, slow_fetch):
    entered, release = slow_fetch
    waiter = asyncio.create_task(stack.node.submit({
        "type": "echo", "arguments": arguments(inline(blob())), "wait": 30,
    }))
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    job_id = next(iter(stack.node.jobs))
    release.set()
    assert (await stack.finish(job_id))["status"] == "done"
