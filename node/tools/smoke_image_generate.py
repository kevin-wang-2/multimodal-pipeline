"""真模型验收：MMP API 生成、常驻引擎回收/恢复、大结果交付和缓存。"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
from mmp_broker.core import Broker
from mmp_broker.inproc import InProcLink
from mmp_node.config import Config
from mmp_node.node import Node


class Handler(BaseHTTPRequestHandler):
    data = b""

    def log_message(self, *args):
        pass

    def do_PUT(self):
        self.__class__.data = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.end_headers()


async def wait_warm(node):
    for _ in range(1200):
        if "triage_image" in node.pool.loaded():
            return
        await asyncio.sleep(.1)
    raise AssertionError("Florence did not recover")


async def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config.model_validate({
            "node": {"id": "krea-smoke", "key": "krea-smoke-key-012345", "data_dir": tmp},
            "scheduler": {"vram_total_mb": 16303, "vram_headroom_mb": 4096,
                          "engine_start_timeout_sec": 120},
            "engines": {
                "triage_image": {"module": "engines.triage_image", "python": os.environ["MMP_IMAGE_PYTHON"],
                                 "timeout_sec": 120, "resident_vram_mb": 512, "keep_warm": True,
                                 "env": {"MMP_FLORENCE_MODEL": os.environ["MMP_FLORENCE_BASE"]}},
                "image_generate": {"module": "engines.image_generate", "python": os.environ["MMP_KREA_PYTHON"],
                                   "timeout_sec": 900, "env": {
                                       "MMP_COMFY_ROOT": os.environ["MMP_COMFY_ROOT"],
                                       "MMP_KREA_RESERVE_VRAM_GB": "4"}},
            }})
        node = Node(cfg, NODE)
        broker = Broker(cfg.node.key)
        link = InProcLink(node, broker)
        await node.start()
        await link.connect()
        states = []

        async def monitor():
            while True:
                states.append(node.pool.loaded())
                await asyncio.sleep(.1)

        monitoring = asyncio.create_task(monitor())
        try:
            await wait_warm(node)
            request = {
                "type": "image.generate",
                "arguments": {
                    "prompt": "A hand painted isometric game village on a grassy island, small timber houses, "
                              "a winding stone path, warm afternoon light, cohesive stylized game concept art, no text",
                    "options": {"seed": 42},
                },
                "delivery": {"image": {"url": f"http://127.0.0.1:{server.server_port}/image.png"}},
            }
            started = time.monotonic()
            code, submitted = await broker.submit(request)
            job_id = submitted["job_id"]
            while submitted["status"] not in {"done", "failed", "cancelled"}:
                code, submitted = await broker.get(job_id, wait=120)
            assert submitted["status"] == "done", submitted
            image = submitted["outputs"]["image"]
            png = Handler.data if image.get("delivered") else base64.b64decode(image["inline"])
            metadata = submitted["outputs"]["metadata"]["value"]
            assert png.startswith(b"\x89PNG\r\n\x1a\n")
            assert metadata["width"] == metadata["height"] == 1024
            destination = Path(os.environ.get("MMP_KREA_SMOKE_OUTPUT", str(Path(tmp) / "game-village.png")))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(png)
            assert "image_generate" not in node.pool.loaded()
            await wait_warm(node)
            assert ["image_generate"] in states, "Florence was not evicted during Krea generation"
            Handler.data = b""
            _, cached = await broker.submit({**request, "delivery": {
                "image": {"url": f"http://127.0.0.1:{server.server_port}/cached-image.png"}}, "wait": 120})
            assert cached["cached"] is True and cached["usage"]["compute_ms"] == 0, cached
            assert Handler.data == png
            print(json.dumps({"status": "done", "wall_s": round(time.monotonic() - started, 2),
                              "source": submitted["source"], "metrics": node.jobs[job_id].engine_metrics,
                              "png_bytes": len(png), "output": str(destination),
                              "krea_unloaded": True, "florence_restored": True, "cache_verified": True}))
        finally:
            monitoring.cancel()
            await asyncio.gather(monitoring, return_exceptions=True)
            await link.close()
            await node.close()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
