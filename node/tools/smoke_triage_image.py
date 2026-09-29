"""triage.image Windows 真模型冒烟：真实图片、缓存、分阶段 fallback 与显存生命周期。"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
from mmp_broker.core import Broker  # noqa: E402
from mmp_broker.inproc import InProcLink  # noqa: E402
from mmp_node.config import Config  # noqa: E402
from mmp_node.node import Node  # noqa: E402
from mmp_node.schemas import validator_for  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    data = b""
    def log_message(self, *args): pass
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Length", str(len(self.data))); self.end_headers()
        self.wfile.write(self.data)


async def wait_loaded(node: Node, name: str, timeout: float = 120) -> None:
    end = time.monotonic() + timeout
    while name not in node.pool.loaded():
        if time.monotonic() > end:
            raise RuntimeError(f"{name} did not become warm")
        await asyncio.sleep(.2)


async def main() -> None:
    image_python = os.environ["MMP_IMAGE_PYTHON"]
    base = os.environ["MMP_FLORENCE_BASE"]
    large = os.environ["MMP_FLORENCE_LARGE"]
    root = Path(os.environ["MMP_SMOKE_IMAGES"])
    images = [p for name in ("car.jpg", "candy.jpg", "cats.jpg") if (p := root / name).exists()]
    invoice = Path(os.environ.get("MMP_SMOKE_DOCUMENT", ""))
    if invoice.is_file(): images.append(invoice)
    if len(images) < 4:
        raise RuntimeError(f"need four smoke images, found {images}")
    cache_tmp = tempfile.TemporaryDirectory()
    pdf = Path(cache_tmp.name) / "document.pdf"
    Image.open(invoice).convert("RGB").save(pdf, "PDF")
    images.append(pdf)
    cfg = Config.model_validate({
        "node": {"id": "image-smoke", "key": "image-smoke-key-012345", "data_dir": cache_tmp.name},
        "scheduler": {"vram_total_mb": 16303, "vram_headroom_mb": 256, "engine_start_timeout_sec": 120},
        "engines": {
            "triage_image": {"module": "engines.triage_image", "python": image_python, "timeout_sec": 120,
                             "resident_vram_mb": 512, "keep_warm": True, "warm_priority": 100,
                             "env": {"MMP_FLORENCE_MODEL": base}},
            "triage_image_large": {"module": "engines.triage_image_large", "python": image_python,
                                   "timeout_sec": 120, "resident_vram_mb": 1536, "run_vram_mb": 2048,
                                   "env": {"MMP_FLORENCE_MODEL": large}},
        }})
    node = Node(cfg, NODE); await node.start()
    broker = Broker(cfg.node.key); link = InProcLink(node, broker); await link.connect()
    failures = []
    try:
        await wait_loaded(node, "triage_image")
        # 模型加载不等于 CUDA 首次推理；用不同参数预热，避免污染正式缓存键。
        data = images[0].read_bytes()
        await broker.submit({"type": "triage.image", "media": {"inline": base64.b64encode(data).decode()},
                             "params": {"region_dedupe_iou": .79}, "wait": 120})
        base_peak = 0.0
        large_seen = False
        for path in images:
            data = path.read_bytes(); t0 = time.monotonic()
            status, body = await broker.submit({"type": "triage.image",
                                                "media": {"inline": base64.b64encode(data).decode()}, "wait": 120})
            wall = time.monotonic() - t0
            digest = body.get("result") or {}
            errs = list(validator_for("urn:mmp:protocol:1:digest").iter_errors(digest))
            print(json.dumps({"image": path.name, "status": status, "wall_s": round(wall, 3),
                              "source": digest.get("source"), "regions": len(digest.get("regions", [])),
                              "metrics": node.jobs.get(body.get("job_id")).engine_metrics if body.get("job_id") in node.jobs else {}},
                             ensure_ascii=False))
            if status != 200 or errs: failures.append(f"{path.name}: status/schema {status} {errs[:1]}")
            if path.suffix.lower() == ".pdf":
                if digest.get("format") != "application/pdf" or digest.get("surfaces", [{}])[0].get("kind") != "page":
                    failures.append("PDF was not represented as page surfaces")
            source = digest.get("source", {})
            if source.get("engine") == "florence-2-base-ft":
                base_peak = max(base_peak, node.jobs[body["job_id"]].engine_metrics.get("vram_peak_mb", 0))
                if wall > 2: failures.append(f"{path.name}: warm base chain {wall:.2f}s > 2s")
            elif source.get("engine") == "florence-2-large-ft":
                large_seen = True
                metrics = node.jobs[body["job_id"]].engine_metrics
                if metrics.get("large_vram_peak_mb", 0) > 2560:
                    failures.append(f"{path.name}: large peak {metrics['large_vram_peak_mb']} MiB > 2560")
                if "fallback_reason" not in source.get("params", {}):
                    failures.append(f"{path.name}: fallback source missing reason")
            base_ms = sum(v for k, v in digest.get("timings_ms", {}).items() if k.startswith("base_"))
            if base_ms and base_ms > 2000:
                failures.append(f"{path.name}: fallback base stage {base_ms}ms > 2s")
        if base_peak == 0: failures.append("base engine did not report VRAM metrics")
        elif base_peak > 1024: failures.append(f"base resident/peak {base_peak} MiB > 1024")

        # inline 后用 get 交付相同字节，必须命中同一个媒体 / 结果缓存并保留来源。
        Handler.data = images[0].read_bytes(); server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            status, cached = await broker.submit({"type": "triage.image", "media": {"get": {
                "url": f"http://127.0.0.1:{server.server_address[1]}/image"}}, "wait": 30})
            if status != 200 or not cached.get("cached"):
                failures.append("inline/get did not share result cache")
        finally:
            server.shutdown()

        if not large_seen: failures.append("real document did not exercise large fallback")
        await wait_loaded(node, "triage_image", 120)
        if "triage_image_large" in node.pool.loaded(): failures.append("large remained resident after fallback")
    finally:
        await link.close(); await node.close(); cache_tmp.cleanup()
    if failures:
        raise SystemExit("\n".join(failures))
    print("triage.image smoke: PASS")


if __name__ == "__main__":
    asyncio.run(main())
