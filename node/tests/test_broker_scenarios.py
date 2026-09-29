"""B 行为契约：protocol/fixtures/broker-scenarios/*.json 用假 A 驱动 B-py 的 Broker。ts 侧同一批场景见 ts/packages/broker/test。"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from mmp_broker.core import Broker, RegisterRejected

SCEN_DIR = Path(__file__).resolve().parents[2] / "protocol" / "fixtures" / "broker-scenarios"
SCENARIOS = sorted(SCEN_DIR.glob("*.json"))


def subset(expected, actual, path="$") -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected object, got {actual!r}"
        for k, v in expected.items():
            assert k in actual, f"{path}.{k} missing in {actual!r}"
            subset(v, actual[k], f"{path}.{k}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected), f"{path}: list length {len(actual) if isinstance(actual, list) else actual!r} != {len(expected)}"
        for i, (e, a) in enumerate(zip(expected, actual)):
            subset(e, a, f"{path}[{i}]")
    else:
        assert expected == actual, f"{path}: {actual!r} != {expected!r}"


class FakeNode:
    """假 A：按 replies 表应答，记录收到的 request。"""

    def __init__(self, node_id: str, replies: dict, delay_sec: float):
        self.node_id, self.replies, self.delay = node_id, replies, delay_sec
        self.received: list[dict] = []

    async def send(self, payload: dict) -> dict:
        self.received.append(payload)
        if self.delay:
            await asyncio.sleep(self.delay)
        rep = self.replies.get(payload["op"])
        if rep is None:
            return {"req_id": payload["req_id"], "http_status": 404, "body": {"error": "not_found"}}
        job_id = payload.get("job_id") or f"{self.node_id}-01J7ZQ9K3W8B6Q4M2N1P5R7S9T"
        body = json.loads(json.dumps(rep["body"]).replace("$echo_job_id", job_id))
        return {"req_id": payload["req_id"], "http_status": rep["http_status"], "body": body}


@pytest.mark.parametrize("path", SCENARIOS, ids=[p.stem for p in SCENARIOS])
async def test_scenario(path: Path):
    sc = json.loads(path.read_text(encoding="utf-8"))
    clock = {"t": 1_700_000_000.0}
    b = sc.get("broker", {})
    broker = Broker(b["node_key"], b.get("protocol_version", "1.2"), b.get("inflight_grace_sec", 5),
                    b.get("heartbeat_interval_sec", 10), b.get("missed_heartbeats", 3), now=lambda: clock["t"])
    fakes: dict[str, FakeNode] = {}
    for i, step in enumerate(sc["steps"]):
        where = f"{path.stem} step {i}"
        if "register" in step:
            env = step["register"]
            node_id = (env.get("payload") or {}).get("node_id", "?")
            fake = FakeNode(node_id, step.get("replies") or {}, step.get("delay_sec", 0))
            if "expect_reject" in step:
                with pytest.raises(RegisterRejected) as e:
                    broker.on_register(env, fake.send)
                assert e.value.code == step["expect_reject"], where
            else:
                broker.on_register(env, fake.send)
                fakes[node_id] = fake
        elif "heartbeat" in step:
            broker.on_heartbeat(step["heartbeat"])
        elif "disconnect" in step:
            broker.on_disconnect(step["disconnect"])
        elif "advance_sec" in step:
            clock["t"] += step["advance_sec"]
        elif "op" in step:
            before = {n: len(f.received) for n, f in fakes.items()}
            op, args = step["op"], step.get("args") or {}
            if op == "submit":
                status, body = await broker.submit(args)
            elif op == "get":
                status, body = await broker.get(args["job_id"], args.get("wait", 0))
            elif op == "cancel":
                status, body = await broker.cancel(args["job_id"], args.get("wait", 0))
            elif op == "capabilities":
                status, body = broker.capabilities()
            elif op == "health":
                status, body = broker.health()
            else:
                raise AssertionError(f"unknown op {op}")
            exp = step.get("expect") or {}
            if "http_status" in exp:
                assert status == exp["http_status"], f"{where}: status {status} body {body}"
            if "body" in exp:
                try:
                    subset(exp["body"], body)
                except AssertionError as e:
                    raise AssertionError(f"{where}: {e}\nfull body: {json.dumps(body, ensure_ascii=False)}") from None
            if "expect_routed_to" in step:
                routed = [n for n, f in fakes.items() if len(f.received) > before.get(n, 0)]
                want = step["expect_routed_to"]
                assert routed == ([want] if want else []), f"{where}: routed to {routed}, expected {want}"
        else:
            raise AssertionError(f"{where}: unknown step {step}")
