"""Reference delivery is node-owned, batched, explicit, and never result-cached."""
import asyncio

import pytest

from conftest import inline
from mmp_node import schemas
from mmp_node.errors import ApiError


def request(stack, refs, endpoints):
    return {"type": "media.deliver", "node_id": stack.node.node_id,
            "arguments": {"media": [{"ref": ref} for ref in refs]},
            "delivery": {"media": endpoints}, "wait": 5}


async def test_discovery_batch_put_and_repeated_delivery(stack, media_server):
    caps = (await stack.client.get("/capabilities")).json()["capabilities"]
    cap = next(entry["capability"] for entry in caps if entry["capability"]["purpose"] == "media.deliver")
    assert not schemas.errors(schemas.validator_for_ref("urn:mmp:protocol:3:capability"), cap)
    data = [b"image bytes", b"audio bytes"]
    refs = [stack.node.media.store(blob, mime) for blob, mime in zip(data, ["image/png", "audio/flac"])]
    job = request(stack, refs, [media_server.put_endpoint(f"/{i}") for i in range(2)])
    for _ in range(2):
        done = (await stack.submit(**job)).json()
        assert done["status"] == "done", done
        assert done["cached"] is False
        assert [item["media_id"] for item in done["outputs"]["media"]] == refs
        assert all(item["delivered"] for item in done["outputs"]["media"])
        assert stack.node.pool.loaded() == []
        assert stack.node.media._pins == {}
        assert [media_server.puts[f"/{i}"] for i in range(2)] == data
        media_server.puts.clear()


async def test_batch_bounded_parallelism_and_failure_cleanup(stack, monkeypatch):
    refs = [stack.node.media.store(bytes([i]), "application/octet-stream") for i in range(9)]
    active = peak = finished = 0

    async def put(endpoint, data, content_type):
        nonlocal active, peak, finished
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            if data == b"\x01":
                raise ApiError("engine_failed", "PUT failed")
        finally:
            active -= 1
            finished += 1

    monkeypatch.setattr(stack.node.media, "put", put)
    done = (await stack.submit(**request(stack, refs, [{"url": f"https://upload.test/{i}"} for i in range(9)]))).json()
    assert done["status"] == "failed" and done["error"] == "engine_failed"
    assert 1 < peak <= 4 and active == 0 and finished == 9
    assert stack.node.media._pins == {}


async def test_invalid_batch_is_rejected_before_put(stack, monkeypatch):
    ref = stack.node.media.store(b"present", "audio/flac")
    endpoints = [{"url": "https://upload.test/one"}, {"url": "https://upload.test/two"}]
    calls = []

    async def put(*args):
        calls.append(args)

    monkeypatch.setattr(stack.node.media, "put", put)
    missing = (await stack.submit(**request(stack, [ref, "sha256:" + "f" * 64], endpoints))).json()
    assert missing["error"] == "media_not_found"
    assert stack.node.media._pins == {} and calls == []
    for refs, targets in [([ref], []), ([ref], endpoints), ([], endpoints)]:
        response = await stack.submit(**request(stack, refs, targets))
        assert response.status_code == 400
    invalid = request(stack, [ref], endpoints[:1])
    invalid["arguments"]["media"] = [inline(b"new input")]
    assert (await stack.submit(**invalid)).status_code == 400
    assert calls == []


async def test_cancel_delivery_releases_all_pins(stack, monkeypatch):
    ref = stack.node.media.store(b"present", "audio/flac")
    entered = asyncio.Event()

    async def put(*args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(stack.node.media, "put", put)
    job = request(stack, [ref], [{"url": "https://upload.test/one"}])
    job["wait"] = 0
    pending = (await stack.submit(**job)).json()
    await asyncio.wait_for(entered.wait(), 2)
    cancelled = (await stack.cancel(pending["job_id"], wait=2)).json()
    assert cancelled["status"] == "cancelled"
    assert stack.node.media._pins == {}
