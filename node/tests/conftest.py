"""测试装置：临时目录里的完整 A + B-py 绑定栈，HTTP 走 ASGI 传输不占端口；另起一个线程 HTTP 服务模拟 get / put 端点。"""
from __future__ import annotations

import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry as RefRegistry, Resource
from referencing.jsonschema import DRAFT202012

from mmp_broker.core import Broker
from mmp_broker.http import create_app
from mmp_broker.inproc import InProcLink
from mmp_node.config import Config
from mmp_node.node import Node

NODE_DIR = Path(__file__).resolve().parents[1]
SCHEMAS = NODE_DIR.parent / "protocol" / "schemas"
NODE_KEY = "test-node-key-0123456789"


# ---------- schema 校验 ----------
def _registry() -> RefRegistry:
    reg = RefRegistry()
    for p in SCHEMAS.glob("*.schema.json"):
        s = json.loads(p.read_text(encoding="utf-8"))
        reg = reg.with_resource(s["$id"], Resource.from_contents(s, default_specification=DRAFT202012))
    return reg


_REG = _registry()


def validator(ref: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": ref}, registry=_REG, format_checker=FormatChecker())


V_ANY = validator("urn:mmp:protocol:1:job-api#/$defs/AnyResponse")
V_ENV = validator("urn:mmp:protocol:1:ab-message")
V_CAPS = validator("urn:mmp:protocol:1:job-api#/$defs/CapabilitiesResponse")
V_HEALTH = validator("urn:mmp:protocol:1:job-api#/$defs/HealthResponse")


def assert_valid(v: Draft202012Validator, data) -> None:
    errs = list(v.iter_errors(data))
    assert not errs, "\n".join(f"{list(e.path)}: {e.message}" for e in errs[:5])


# ---------- 配置 ----------
def make_config(tmp_path: Path, engines: dict | None = None, **over) -> Config:
    data = {
        "node": {"id": "t-node", "key": NODE_KEY, "data_dir": str(tmp_path / "cache")},
        "queue": {"max_len": 500, "retry_after_sec": 1},
        "scheduler": {"idle_unload_sec": 300, "engine_start_timeout_sec": 30},
        "media": {"max_inline_result_bytes": 4096},
        "jobs": {"retention_sec": 3600},
        "broker": {"inflight_grace_sec": 2},
        "engines": engines if engines is not None else {"echo": {"module": "engines.echo", "timeout_sec": 5}},
    }
    for k, v in over.items():
        sec, key = k.split("__")
        if sec == "engines":
            for e in data["engines"].values():
                e[key] = v
        else:
            data.setdefault(sec, {})[key] = v
    return Config.model_validate(data)


class Stack:
    def __init__(self, node: Node, broker: Broker, link: InProcLink, client: httpx.AsyncClient):
        self.node, self.broker, self.link, self.client = node, broker, link, client

    async def submit(self, **job) -> httpx.Response:
        r = await self.client.post("/jobs", json=job)
        assert_valid(V_ANY, r.json())
        return r

    async def get(self, job_id: str, wait: int = 0) -> httpx.Response:
        r = await self.client.get(f"/jobs/{job_id}", params={"wait": wait})
        assert_valid(V_ANY, r.json())
        return r

    async def cancel(self, job_id: str, wait: int = 0) -> httpx.Response:
        r = await self.client.delete(f"/jobs/{job_id}", params={"wait": wait})
        assert_valid(V_ANY, r.json())
        return r

    async def finish(self, job_id: str, timeout: int = 10) -> dict:
        """轮询到终态。"""
        for _ in range(timeout * 4):
            r = await self.get(job_id, wait=1)
            if r.json()["status"] in ("done", "failed", "cancelled"):
                return r.json()
        raise AssertionError(f"{job_id} never finished")


async def build_stack(tmp_path: Path, api_key: str = "", engines: dict | None = None, **over) -> Stack:
    cfg = make_config(tmp_path, engines, **over)
    node = Node(cfg, NODE_DIR)
    await node.start()
    broker = Broker(NODE_KEY, inflight_grace_sec=cfg.broker.inflight_grace_sec)
    link = InProcLink(node, broker, heartbeat_interval_sec=0.2)
    await link.connect()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(broker, api_key)), base_url="http://b")
    return Stack(node, broker, link, client)


@pytest.fixture
async def stack(tmp_path):
    s = await build_stack(tmp_path)
    yield s
    await s.client.aclose()
    await s.link.close()
    await s.node.close()


# ---------- 媒体 ----------
def inline(data: bytes) -> dict:
    return {"inline": base64.b64encode(data).decode()}


def blob(n: int = 2000, seed: int = 0) -> bytes:
    return bytes((i * 7 + seed) % 251 for i in range(n))


class _Handler(BaseHTTPRequestHandler):
    files: dict[str, bytes] = {}
    puts: dict[str, bytes] = {}
    require_header: tuple[str, str] | None = None

    def log_message(self, *a):  # 静音
        pass

    def do_GET(self):
        if self.require_header and self.headers.get(self.require_header[0]) != self.require_header[1]:
            self.send_response(403); self.end_headers(); return
        data = self.files.get(self.path)
        if data is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_PUT(self):
        n = int(self.headers.get("Content-Length", "0"))
        self.puts[self.path] = self.rfile.read(n)
        self.send_response(201); self.end_headers()


class MediaServer:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        _Handler.files, _Handler.puts, _Handler.require_header = {}, {}, None

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def serve(self, path: str, data: bytes) -> dict:
        _Handler.files[path] = data
        return {"url": self.url(path)}

    def put_endpoint(self, path: str) -> dict:
        return {"url": self.url(path)}

    @property
    def puts(self) -> dict[str, bytes]:
        return _Handler.puts

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def media_server():
    s = MediaServer()
    yield s
    s.close()
