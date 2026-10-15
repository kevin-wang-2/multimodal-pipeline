"""Real-model acceptance for Qwen internal generation, edit, media ref reuse, and result cache."""
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
    outputs: dict[str, bytes] = {}

    def log_message(self, *args):
        pass

    def do_PUT(self):
        self.__class__.outputs[self.path] = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.end_headers()


async def finish(broker: Broker, request: dict) -> tuple[dict, float]:
    started = time.monotonic()
    _, job = await broker.submit(request)
    while job["status"] not in {"done", "failed", "cancelled"}:
        _, job = await broker.get(job["job_id"], wait=120)
    assert job["status"] == "done", job
    return job, time.monotonic() - started


async def main() -> None:
    source = Path(os.environ["MMP_QWEN_SMOKE_INPUT"])
    source_content_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }[source.suffix.lower()]
    output_dir = Path(os.environ["MMP_QWEN_SMOKE_OUTPUT"])
    output_dir.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as temporary:
        cfg = Config.model_validate({
            "node": {"id": "qwen-smoke", "key": "qwen-smoke-key-012345", "data_dir": temporary},
            "scheduler": {"vram_total_mb": 16303, "vram_headroom_mb": 1024,
                          "engine_start_timeout_sec": 120},
            "engines": {
                "qwen_image_internal": {
                    "module": "engines.qwen_image_internal",
                    "python": os.environ["MMP_QWEN_PYTHON"],
                    "timeout_sec": 900,
                    "env": {"MMP_QWEN_COMFY_ROOT": os.environ["MMP_QWEN_COMFY_ROOT"],
                            "MMP_QWEN_RESERVE_VRAM_GB": "1"},
                },
            },
        })
        node = Node(cfg, NODE)
        broker = Broker(cfg.node.key)
        link = InProcLink(node, broker)
        await node.start()
        await link.connect()

        def delivery(name: str) -> dict:
            return {"image": {"url": f"http://127.0.0.1:{server.server_port}/{name}"}}

        records = []
        try:
            caps = {item["capability"]["id"] for item in broker.capabilities()[1]["capabilities"]}
            assert caps == {"image.generate.qwen", "image.edit.qwen", "media.deliver"}
            assert [cap["id"] for cap in node.register_message()["payload"]["capabilities"]] == ["media.deliver"]

            generated, wall = await finish(broker, {
                "type": "image.generate.qwen",
                "arguments": {"prompt": "A clean editorial photograph of a red ceramic mug on a pale wooden desk, "
                                        "a small green plant in the background, soft daylight, no text.",
                              "options": {"width": 768, "height": 1344, "seed": 561001}},
                "delivery": delivery("01-generate-768x1344.png"),
            })
            assert generated["outputs"]["metadata"]["value"]["width"] == 768
            assert generated["outputs"]["metadata"]["value"]["height"] == 1344
            records.append(("generate", generated, wall))

            edited, wall = await finish(broker, {
                "type": "image.edit.qwen",
                "arguments": {"image": {"inline": base64.b64encode(source.read_bytes()).decode(),
                                         "content_type": source_content_type},
                              "prompt": "Replace only the mint-green car body paint with vivid red paint. Preserve "
                                        "the car, wheels, windows, framing, lighting, street and background.",
                              "options": {"resolution": 1024, "seed": 561002}},
                "delivery": delivery("02-edit-existing.png"),
            })
            records.append(("edit", edited, wall))

            previous = edited["outputs"]["image"]["media_id"]
            continued_request = {
                "type": "image.edit.qwen",
                "arguments": {"image": {"ref": previous, "content_type": "image/png"},
                              "prompt": "Add one thin gold racing stripe along the side of the red car body. Keep "
                                        "the red paint and preserve the car, wheels, windows, framing, lighting, street and background.",
                              "options": {"resolution": 1024, "seed": 561003}},
                "delivery": delivery("03-edit-previous-output.png"),
            }
            fetch_media = node.media.fetch
            fetched_media = []

            async def track_fetch(handle):
                result = await fetch_media(handle)
                fetched_media.append(result)
                return result

            node.media.fetch = track_fetch
            try:
                continued, wall = await finish(broker, continued_request)
            finally:
                node.media.fetch = fetch_media
            assert len(fetched_media) == 1
            assert fetched_media[0].media_id == previous
            assert fetched_media[0].fetched is False
            records.append(("continue", continued, wall))

            cached, wall = await finish(broker, {
                **continued_request,
                "delivery": delivery("04-cached-edit.png"),
            })
            assert cached["cached"] is True and cached["usage"]["compute_ms"] == 0
            records.append(("cached", cached, wall))

            for name, data in Handler.outputs.items():
                assert data.startswith(b"\x89PNG\r\n\x1a\n")
                (output_dir / name.removeprefix("/")).write_bytes(data)
            assert Handler.outputs["/03-edit-previous-output.png"] == Handler.outputs["/04-cached-edit.png"]
            assert node.pool.loaded() == []
            print(json.dumps({
                "status": "done",
                "outputs": sorted(path.removeprefix("/") for path in Handler.outputs),
                "runs": [{
                    "name": name,
                    "wall_s": round(wall, 2),
                    "cached": job["cached"],
                    "media_id": job["outputs"]["image"]["media_id"],
                    "metadata": job["outputs"]["metadata"]["value"],
                    "usage": job["usage"],
                    "engine_metrics": node.jobs[job["job_id"]].engine_metrics,
                    "fetch_ms": job["timings_ms"].get("fetch"),
                } for name, job, wall in records],
                "engine_unloaded": True,
                "remote_registration_hidden": True,
            }, ensure_ascii=False))
        finally:
            await link.close()
            await node.close()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
