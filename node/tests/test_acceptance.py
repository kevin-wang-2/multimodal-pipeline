"""S1 验收（实施计划 §3 S1 + issue #2）。全部经 HTTP 绑定走完整栈：HTTP → Broker → 进程内信封 → Node → echo 子进程。"""
import asyncio

import pytest

from conftest import V_CAPS, V_ENV, V_HEALTH, arguments, assert_valid, blob, build_stack, inline, media_id, output


async def test_submit_wait_done_and_cached(stack):
    r = await stack.submit(type="echo", arguments=arguments(inline(blob()), {"tag": "a"}), wait=5)
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "done" and b["cached"] is False
    assert b["source"]["tier"] == "cpu" and b["source"]["degraded"] is False
    assert b["usage"]["served_from"] == "compute"
    assert b["usage"]["tier"] == "cpu" and b["usage"]["engine"] == "echo"
    assert b["usage"]["compute_ms"] >= 0 and b["usage"]["wasted_ms"] == 0
    assert output(b)["bytes"] == 2000 and output(b)["media_id"] == media_id(b)

    r2 = await stack.submit(type="echo", arguments=arguments(inline(blob()), {"tag": "a"}), wait=5)
    assert r2.status_code == 200 and r2.json()["cached"] is True
    assert r2.json()["usage"] == {"served_from": "cache", "tier": "cpu", "engine": "echo",
                                  "compute_ms": 0, "wasted_ms": 0}
    assert set(r2.json()["timings_ms"]) == {"fetch"}
    assert media_id(r2.json()) == media_id(b)
    assert r2.json()["job_id"] != b["job_id"]  # 新交付、新 id，结果来自缓存

    # 参数不同 → 缓存键不同 → 重算
    r3 = await stack.submit(type="echo", arguments=arguments(inline(blob()), {"tag": "b"}), wait=5)
    assert r3.json()["cached"] is False


async def test_inline_and_get_share_media_id(stack, media_server):
    data = blob(5000, seed=3)
    r1 = await stack.submit(type="echo", arguments=arguments(inline(data)), wait=5)
    r2 = await stack.submit(type="echo", arguments=arguments({"get": media_server.serve("/a.bin", data)}), wait=5)
    assert r1.status_code == r2.status_code == 200
    assert media_id(r1.json()) == media_id(r2.json())
    assert r2.json()["cached"] is True  # 同键 → 命中 inline 那次的结果


async def test_get_with_auth_header_and_failures(stack, media_server):
    from conftest import _Handler
    data = blob(100)
    _Handler.require_header = ("X-Token", "s3cret")
    ep = media_server.serve("/p.bin", data)
    r = await stack.submit(type="echo", arguments=arguments({"get": ep}), wait=5)
    assert r.status_code == 422 and r.json()["error"] == "media_fetch_failed"
    r = await stack.submit(type="echo", arguments=arguments({"get": {**ep, "headers": {"X-Token": "s3cret"}}}), wait=5)
    assert r.status_code == 200
    _Handler.require_header = None
    r = await stack.submit(type="echo", arguments=arguments({"get": {"url": media_server.url("/missing")}}), wait=5)
    assert r.status_code == 422


async def test_priority_interactive_before_batch(tmp_path):
    """并发提交 100 个 echo：单并发引擎，先塞 50 个 batch 再塞 50 个 interactive，interactive 必须全部先跑。"""
    s = await build_stack(tmp_path, engines__max_concurrency=1)
    try:
        blocker = await s.submit(type="echo", arguments=arguments(inline(blob(10)), {"sleep_ms": 400, "tag": "blocker"}))
        assert blocker.status_code == 202
        batch = [s.submit(type="echo", arguments=arguments(inline(blob(10)), {"tag": f"b{i}"}), priority="batch") for i in range(50)]
        ids_b = [r.json()["job_id"] for r in await asyncio.gather(*batch)]
        inter = [s.submit(type="echo", arguments=arguments(inline(blob(10)), {"tag": f"i{i}"}), priority="interactive") for i in range(50)]
        ids_i = [r.json()["job_id"] for r in await asyncio.gather(*inter)]
        assert len(set(ids_b + ids_i)) == 100

        started_b = [output(await s.finish(j))["started_at_ms"] for j in ids_b]
        started_i = [output(await s.finish(j))["started_at_ms"] for j in ids_i]
        assert max(started_i) <= min(started_b), "some batch job ran before an interactive one"
        # 同优先级内保持提交顺序
        assert started_i == sorted(started_i) and started_b == sorted(started_b)
    finally:
        await s.client.aclose(); await s.link.close(); await s.node.close()


async def test_backpressure_429(tmp_path):
    s = await build_stack(tmp_path, queue__max_len=3, engines__max_concurrency=1)
    try:
        blocker = (await s.submit(type="echo", arguments=arguments(inline(blob(10)), {"sleep_ms": 3000, "tag": "run"}))).json()["job_id"]
        while (await s.get(blocker)).json()["status"] != "running":
            await asyncio.sleep(0.02)
        queued = [await s.submit(type="echo", arguments=arguments(inline(blob(10)), {"tag": f"q{i}"})) for i in range(3)]
        assert all(r.status_code == 202 and r.json()["status"] == "queued" for r in queued)
        assert [r.json()["queue_position"] for r in queued] == [0, 1, 2]
        r = await s.submit(type="echo", arguments=arguments(inline(blob(10)), {"tag": "overflow"}))
        assert r.status_code == 429
        assert r.json()["error"] == "backpressure" and r.json()["retry_after_sec"] == 1
        # 取消一个排队的，位置腾出来
        c = await s.cancel(queued[0].json()["job_id"])
        assert c.status_code == 200 and c.json()["status"] == "cancelled"
        r = await s.submit(type="echo", arguments=arguments(inline(blob(10)), {"tag": "fits-now"}))
        assert r.status_code == 202
    finally:
        await s.client.aclose(); await s.link.close(); await s.node.close()


async def test_dedup_merges_identical_inflight(stack):
    rs = await asyncio.gather(*[stack.submit(type="echo", arguments=arguments(inline(blob(10)), {"sleep_ms": 300, "tag": "same"})) for _ in range(5)])
    ids = {r.json()["job_id"] for r in rs}
    assert len(ids) == 1, "identical in-flight submits must merge into one job"
    assert (await stack.finish(ids.pop()))["status"] == "done"


async def test_multiple_named_media_inputs_are_fetched_keyed_and_unpinned(tmp_path, monkeypatch):
    import sys
    import types
    from mmp_node.engine_io import EngineResult

    module = types.ModuleType("multi_input_test")
    module.CAPABILITIES = [{
        "id": "image.compose", "visibility": "public", "purpose": "image.compose",
        "tiers": [{"tier": "cpu", "engine": "compose", "engine_version": "1", "cost": "low"}],
        "input": {"fields": [
            {"name": "base", "kind": "media", "required": True, "accepts": ["image/*"],
             "cardinality": {"min": 1, "max": 1}},
            {"name": "references", "kind": "media", "required": True, "accepts": ["image/*"],
             "cardinality": {"min": 1, "max": 3}},
            {"name": "prompt", "kind": "text", "required": True, "schema": {"type": "string", "minLength": 1}},
        ]},
        "output": {"fields": [
            {"name": "gallery", "kind": "media", "required": True, "produces": ["image/png"],
             "cardinality": {"min": 1, "max": 2}},
            {"name": "caption", "kind": "text", "required": True,
             "schema": {"type": "string", "minLength": 1}},
            {"name": "summary", "kind": "json", "required": True, "schema": {"type": "object"}},
        ], "agent_context": "none"},
    }]
    monkeypatch.setitem(sys.modules, "multi_input_test", module)
    stack = await build_stack(tmp_path, engines={"compose": {"module": "multi_input_test", "timeout_sec": 5}})

    async def fake_run(_spec, job, _timeout, _vram):
        assert len(job["arguments"]["references"]) == 2
        return EngineResult({
            "gallery": [
                {"kind": "media", "content_type": "image/png", "data_base64": "YQ=="},
                {"kind": "media", "content_type": "image/png", "data_base64": "Yg=="},
            ],
            "caption": {"kind": "text", "text": "" if job["arguments"]["prompt"] == "bad" else "combined"},
            "summary": {"kind": "json", "value": {
                "base": job["arguments"]["base"]["media_id"],
                "references": [item["media_id"] for item in job["arguments"]["references"]],
            }},
        }, {})

    stack.node.pool.run = fake_run
    delivered = []

    async def fake_put(endpoint, data, content_type):
        delivered.append((endpoint["url"], data, content_type))

    stack.node.media.put = fake_put
    try:
        bad_input = await stack.submit(type="image.compose", arguments={
            "base": {**inline(b"base"), "content_type": "image/png"},
            "references": [{**inline(b"one"), "content_type": "image/png"}],
            "prompt": 1,
        })
        assert bad_input.status_code == 400 and bad_input.json()["error"] == "bad_request"
        request = {"type": "image.compose", "arguments": {
            "base": {**inline(b"base"), "content_type": "image/png"},
            "references": [{**inline(b"one"), "content_type": "image/png"},
                           {**inline(b"two"), "content_type": "image/png"}],
            "prompt": "combine",
        }, "delivery": {"gallery": [{"url": "https://upload.example/one"},
                                       {"url": "https://upload.example/two"}]}, "wait": 5}
        done = (await stack.submit(**request)).json()
        assert done["status"] == "done"
        assert isinstance(done["media_ids"]["references"], list) and len(done["media_ids"]["references"]) == 2
        assert [item["delivered"] for item in done["outputs"]["gallery"]] == [True, True]
        assert done["outputs"]["caption"] == {"kind": "text", "text": "combined"}
        assert [item[0] for item in delivered] == ["https://upload.example/one", "https://upload.example/two"]
        assert done["outputs"]["summary"]["value"]["base"] == done["media_ids"]["base"]
        assert stack.node.media._pins == {}
        assert (await stack.submit(**request)).json()["cached"] is True
        invalid = {**request, "arguments": {**request["arguments"], "prompt": "bad"}}
        failed = (await stack.submit(**invalid)).json()
        assert failed["status"] == "failed" and failed["error"] == "engine_failed"
    finally:
        await stack.client.aclose(); await stack.link.close(); await stack.node.close()


async def test_cancel_running_is_best_effort(stack):
    r = await stack.submit(type="echo", arguments=arguments(inline(blob(10)), {"sleep_ms": 2000, "tag": "c"}))
    jid = r.json()["job_id"]
    for _ in range(20):
        if (await stack.get(jid)).json()["status"] == "running":
            break
        await asyncio.sleep(0.05)
    c = await stack.cancel(jid, wait=2)
    assert c.status_code == 200 and c.json()["status"] == "cancelled"
    # 终态不再变化
    assert (await stack.get(jid)).json()["status"] == "cancelled"
    assert (await stack.cancel(jid)).json()["status"] == "cancelled"


async def test_engine_failure_and_timeout(tmp_path):
    s = await build_stack(tmp_path, engines__timeout_sec=0.4)
    try:
        r = await s.submit(type="echo", arguments=arguments(inline(blob(10)), {"fail": True}), wait=5)
        assert r.status_code == 500 and r.json() == {**r.json(), "status": "failed", "error": "engine_failed"}
        r = await s.submit(type="echo", arguments=arguments(inline(blob(10)), {"sleep_ms": 1500}), wait=5)
        assert r.status_code == 504 and r.json()["error"] == "timeout"
        # 引擎没被拖死：后面的任务照常
        r = await s.submit(type="echo", arguments=arguments(inline(blob(10)), {"tag": "after"}), wait=5)
        assert r.status_code == 200
    finally:
        await s.client.aclose(); await s.link.close(); await s.node.close()


async def test_timeout_fallback_records_final_compute_and_wasted_attempt(tmp_path):
    from mmp_node.engine_io import EngineResult, EngineTimeout

    s = await build_stack(tmp_path)
    try:
        cap = s.node.registry.types["echo"]
        gpu = {"tier": "gpu", "engine": "echo-gpu", "engine_version": "1", "cost": "high",
               "latency_hint": "test", "max_concurrency": 1}
        cap["tiers"] = [gpu, *cap["tiers"]]
        s.node.registry.type_engine[("echo", "gpu")] = "echo"

        async def fake_run(_spec, job, _timeout, _vram):
            if job["tier"] == "gpu":
                raise EngineTimeout(37)
            media = job["arguments"]["media"]
            return EngineResult({"result": {"kind": "json", "value": {
                "media_id": media["media_id"], "bytes": 10, "options": job["arguments"].get("options", {}),
                "started_at_ms": 1}}}, {}, compute_ms=11)

        s.node.pool.run = fake_run
        response = await s.submit(type="echo", tier="gpu", arguments=arguments(inline(blob(10))), wait=5)
        body = response.json()
        assert response.status_code == 200
        assert body["source"]["tier"] == "cpu" and body["source"]["degraded_reason"] == "timeout"
        assert body["usage"] == {"served_from": "compute", "tier": "cpu", "engine": "echo",
                                 "compute_ms": 11, "wasted_ms": 37}
    finally:
        await s.client.aclose(); await s.link.close(); await s.node.close()


async def test_request_validation(stack):
    r = await stack.submit(type="echo", arguments=arguments(inline(blob(10)), {"nope": 1}), wait=1)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    r = await stack.submit(type="nothing.here", arguments=arguments(inline(blob(10))))
    assert r.status_code == 503 and r.json()["error"] == "no_node"
    r = await stack.submit(type="echo", arguments=arguments(inline(blob(10))), tier="gpu")
    assert r.status_code == 400
    r = await stack.submit(type="echo", arguments=arguments({"path": "/etc/passwd"}))
    assert r.status_code == 400
    r = await stack.submit(type="echo", arguments=arguments({"inline": "!!not-base64!!"}))
    assert r.status_code == 400
    r = await stack.client.post("/jobs", content=b"{not json")
    assert r.status_code == 400
    r = await stack.get("t-node-01J7ZQ9K3W8B6Q4M2N1P5R7S9T")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    r = await stack.get("other-01J7ZQ9K3W8B6Q4M2N1P5R7S9T")
    assert r.status_code == 503 and r.json()["error"] == "node_offline"


async def test_inline_too_large(tmp_path):
    s = await build_stack(tmp_path, media__max_inline_bytes=1000)
    try:
        r = await s.submit(type="echo", arguments=arguments(inline(blob(1500))))
        assert r.status_code == 413 and r.json()["error"] == "media_too_large"
    finally:
        await s.client.aclose(); await s.link.close(); await s.node.close()


async def test_json_output_is_inline_and_rejects_delivery(stack, media_server):
    r = await stack.submit(type="echo", arguments=arguments(inline(blob(50)), {"pad_bytes": 10000}), wait=5)
    assert r.status_code == 200 and len(output(r.json())["pad"]) == 10000
    rejected = await stack.submit(type="echo", arguments=arguments(inline(blob(50))),
                                  delivery={"result": media_server.put_endpoint("/out.json")})
    assert rejected.status_code == 400 and rejected.json()["error"] == "bad_request"


async def test_capabilities_and_health(stack):
    r = await stack.client.get("/capabilities")
    assert_valid(V_CAPS, r.json())
    assert r.json()["capabilities"][0]["capability"]["id"] == "echo"
    assert r.json()["capabilities"][0]["nodes"] == ["t-node"]
    r = await stack.client.get("/health")
    assert_valid(V_HEALTH, r.json())
    assert r.json()["status"] == "ok"
    await asyncio.sleep(0.5)  # 心跳跑起来
    assert "engines_loaded" in (await stack.client.get("/health")).json()["nodes"][0]["heartbeat"]


async def test_ab_envelopes_validate_against_schema(stack):
    assert_valid(V_ENV, stack.node.register_message())
    assert_valid(V_ENV, stack.node.heartbeat_message())
    req = stack.node.envelope("request", {"req_id": "r1", "op": "submit",
                                         "job": {"type": "echo", "arguments": arguments(inline(blob(10))), "wait": 5}})
    assert_valid(V_ENV, req)
    resp = stack.node.envelope("response", await stack.node.dispatch(req["payload"]))
    assert_valid(V_ENV, resp)
    assert resp["payload"]["http_status"] == 200 and resp["payload"]["req_id"] == "r1"
    bad = await stack.node.dispatch({"req_id": "r2", "op": "get", "job_id": "t-node-01J7ZQ9K3W8B6Q4M2N1P5R7S9T"})
    assert_valid(V_ENV, stack.node.envelope("response", bad))
    assert bad["http_status"] == 404


async def test_register_rejections():
    from mmp_broker.core import Broker, RegisterRejected
    b = Broker("right-key-0123456789")
    async def send(_): return {}
    env = {"type": "register", "protocol_version": "3.0", "ts": "2026-09-20T00:00:00Z",
           "payload": {"node_id": "n", "node_key": "wrong-key-0123456789", "capabilities": [{"id": "x"}], "engine_versions": {}}}
    with pytest.raises(RegisterRejected) as e:
        b.on_register(env, send)
    assert e.value.code == 4001
    with pytest.raises(RegisterRejected) as e:
        b.on_register({**env, "protocol_version": "2.0"}, send)
    assert e.value.code == 4002
    with pytest.raises(RegisterRejected) as e:
        b.on_register({**env, "type": "heartbeat"}, send)
    assert e.value.code == 4003


async def test_api_key(tmp_path):
    s = await build_stack(tmp_path, api_key="k")
    try:
        assert (await s.client.get("/capabilities")).status_code == 401
        assert (await s.client.get("/capabilities", headers={"Authorization": "Bearer k"})).status_code == 200
        assert (await s.client.get("/health")).status_code == 200  # health 不鉴权
    finally:
        await s.client.aclose(); await s.link.close(); await s.node.close()


# ---------- 资源 cache：ref 引用 + LRU（协议 v1.1）----------
async def test_media_ref_skips_refetch(stack, media_server):
    from conftest import _Handler
    data = blob(4000, seed=11)
    ep = media_server.serve("/big.wav", data)
    r1 = await stack.submit(type="echo", arguments=arguments({"get": ep}, {"tag": "first"}), wait=5)
    assert r1.status_code == 200 and r1.json()["cached"] is False
    mid = media_id(r1.json())
    hits_after_first = _Handler.gets
    # 只给 ref：不同 options → 不命中结果缓存，但媒体不再下载
    r2 = await stack.submit(type="echo", arguments=arguments({"ref": mid}, {"tag": "second"}), wait=5)
    assert r2.status_code == 200 and r2.json()["cached"] is False and media_id(r2.json()) == mid
    assert _Handler.gets == hits_after_first
    assert r2.json()["timings_ms"]["fetch"] <= 100   # 命中只是 SQLite touch + stat（Windows 上 ~16ms）；真正的不变量是上一行的 GET 计数
    # ref + get：本地有 → 也不下载
    r3 = await stack.submit(type="echo", arguments=arguments({"ref": mid, "get": ep}, {"tag": "third"}), wait=5)
    assert r3.status_code == 200 and _Handler.gets == hits_after_first
    # 不认识的 ref 且没有来源 → 422 media_not_found（C 应退回带完整句柄重提）
    r4 = await stack.submit(type="echo", arguments=arguments({"ref": "sha256:" + "0" * 64}), wait=5)
    assert r4.status_code == 422 and r4.json()["error"] == "media_not_found"
    # ref + get 但内容对不上 → 422 media_hash_mismatch（内容本身仍以真实 hash 入库）
    r5 = await stack.submit(type="echo", arguments=arguments({"ref": "sha256:" + "0" * 64, "get": ep}), wait=5)
    assert r5.status_code == 422 and r5.json()["error"] == "media_hash_mismatch"
    assert _Handler.gets == hits_after_first + 1
    # inline 也一样：ref 命中就不用再传 8MB
    r6 = await stack.submit(type="echo", arguments=arguments({"ref": mid, "inline": "AAAA"}, {"tag": "fourth"}), wait=5)
    assert r6.status_code == 200 and media_id(r6.json()) == mid


async def test_media_store_lru_eviction_respects_pins(tmp_path):
    s = await build_stack(tmp_path, media__max_store_bytes=10_000, engines__max_concurrency=1)
    try:
        store = s.node.media
        a = media_id((await s.submit(type="echo", arguments=arguments(inline(blob(4000, seed=1))), wait=5)).json())
        b = media_id((await s.submit(type="echo", arguments=arguments(inline(blob(4000, seed=2))), wait=5)).json())
        assert store.has(a) and store.has(b) and store.total_bytes() == 8000
        # 第三份进来超限 → 淘汰最久未访问的 a
        c = media_id((await s.submit(type="echo", arguments=arguments(inline(blob(4000, seed=3))), wait=5)).json())
        assert not store.has(a) and store.has(b) and store.has(c)
        # 被淘汰的 ref 再来 → media_not_found；带来源就重新拉回
        r = await s.submit(type="echo", arguments=arguments({"ref": a}, {"tag": "x"}), wait=5)
        assert r.status_code == 422 and r.json()["error"] == "media_not_found"
        # 在跑的任务 pin 住：d 在 sleep，塞入 e、f 不能把 d 淘汰
        r = await s.submit(type="echo", arguments=arguments(inline(blob(4000, seed=4)), {"sleep_ms": 1500}))
        d = media_id(r.json())
        for seed in (5, 6):
            await s.submit(type="echo", arguments=arguments(inline(blob(4000, seed=seed)), {"sleep_ms": 1500}))
        assert store.has(d), "pinned media must survive eviction"
        assert store.total_bytes() <= 10_000 + 4000  # 允许 pin 导致的短暂超限
        d_job = await s.finish(r.json()["job_id"])
        assert d_job["status"] == "done"
    finally:
        await s.client.aclose(); await s.link.close(); await s.node.close()
