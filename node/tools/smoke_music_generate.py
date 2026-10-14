"""Real-model acceptance: music generation, score reuse, PUT/cache, cancel and GPU recovery."""
from __future__ import annotations

import asyncio
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
    files: dict[str, bytes] = {}

    def log_message(self, *args):
        pass

    def do_PUT(self):
        self.__class__.files[self.path] = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.end_headers()


async def main():
    python = os.environ["MMP_YUE2_PYTHON"]
    root = os.environ["MMP_YUE2_ROOT"]
    destination = Path(os.environ.get("MMP_YUE2_SMOKE_OUTPUT", "ci-out/yue2"))
    destination.mkdir(parents=True, exist_ok=True)
    source = os.environ.get("MMP_YUE2_SMOKE_REQUEST")
    raw = json.loads(Path(source).read_text(encoding="utf-8")) if source else {
        "style": "English, warm piano pop, expressive female vocal, bass and light drums, 88 BPM",
        "lyrics": "[Verse]\nWhen the lamps begin to glow\nWe take the road we used to know\n"
                  "Every step becomes a song\nThe quiet night will sing along\n\n"
                  "[Chorus]\nKeep a little light for me\nAcross the dark and open sea\n"
                  "Let the morning find us here\nA little closer, year by year",
        "seed": 831001,
    }
    arguments = {"style": raw["style"], "lyrics": raw["lyrics"],
                 "options": {"seed": raw.get("seed", 831001)}}
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Handler.files = {}
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as temporary:
        cfg = Config.model_validate({
            "node": {"id": "music-smoke", "key": "music-smoke-key-012345", "data_dir": temporary},
            "scheduler": {"vram_total_mb": 16303, "vram_headroom_mb": 1024,
                          "engine_start_timeout_sec": 120},
            "engines": {"music_generate": {"module": "engines.music_generate", "python": python,
                                            "timeout_sec": 900, "env": {"MMP_YUE2_ROOT": root}}},
        })
        node = Node(cfg, NODE)
        broker = Broker(cfg.node.key)
        link = InProcLink(node, broker)
        await node.start()
        await link.connect()

        def body(args, name):
            return {"type": "music.generate", "arguments": args,
                    "delivery": {"audio": {"url": f"http://127.0.0.1:{server.server_port}/{name}.flac"}}}

        async def finish(job):
            while job["status"] not in {"done", "failed", "cancelled"}:
                _, job = await broker.get(job["job_id"], wait=120)
            assert job["status"] == "done", job
            assert node.pool.loaded() == [], "music process must exit before the job finishes"
            value = job["outputs"]["metadata"]["value"]
            assert value["attention"] == "cudnn" and value["ar_execution"] == "cuda_graph"
            assert not any(value["truncated"].values()), value
            assert value["duration_sec"] > 5 and value["sample_rate"] == 48000 and value["channels"] == 2
            return job

        try:
            caps = {item["capability"]["id"] for item in broker.capabilities()[1]["capabilities"]}
            assert caps == {"music.generate"}, caps
            _, submitted = await broker.submit(body(arguments, "song"))
            _, queued = await broker.submit(body({**arguments, "options": {"seed": 42}}, "queued"))
            assert queued["status"] == "queued", queued
            _, cancelled = await broker.cancel(queued["job_id"])
            assert cancelled["status"] == "cancelled", cancelled
            done = await finish(submitted)
            audio = Handler.files["/song.flac"]
            assert audio.startswith(b"fLaC") and done["outputs"]["audio"]["delivered"]
            (destination / "audio.flac").write_bytes(audio)
            score = done["outputs"]["score"]["text"]
            (destination / "score.abc").write_text(score, encoding="utf-8")
            _, cached = await broker.submit({**body(arguments, "cached"), "wait": 120})
            assert cached["cached"] and cached["usage"]["compute_ms"] == 0, cached
            assert Handler.files["/cached.flac"] == audio

            _, revised = await broker.submit(body({**arguments, "score": score}, "score-reuse"))
            revised = await finish(revised)
            assert not revised["cached"] and revised["outputs"]["score"]["text"] == score
            (destination / "score-reuse.flac").write_bytes(Handler.files["/score-reuse.flac"])

            _, running = await broker.submit(body({**arguments, "options": {"seed": 43}}, "cancelled"))
            for _ in range(1200):
                process = node.pool._procs.get("music_generate")
                if process is not None and process.busy:
                    break
                await asyncio.sleep(.1)
            else:
                raise AssertionError("music engine never started")
            await asyncio.sleep(15)  # Cancel while the native model is generating.
            _, cancelled = await broker.cancel(running["job_id"], wait=30)
            assert cancelled["status"] == "cancelled", cancelled
            for _ in range(200):
                if not node.pool.loaded() and not node.pool._leases.get("music_generate", 0):
                    break
                await asyncio.sleep(.1)
            else:
                raise AssertionError("cancelled engine did not release its GPU lease")
            assert "/cancelled.flac" not in Handler.files and "/queued.flac" not in Handler.files
            # Verify that the cancelled job leaves capacity available for the next generation.
            _, recovered = await broker.submit(body({**arguments, "score": score, "options": {"seed": 44}}, "recovered"))
            await finish(recovered)
            (destination / "result.json").write_text(json.dumps(done, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"status": "done", "wall_s": round(time.monotonic() - started, 2),
                              "metadata": done["outputs"]["metadata"]["value"],
                              "metrics": node.jobs[done["job_id"]].engine_metrics,
                              "cache_verified": True, "score_reuse_verified": True,
                              "queued_cancel_verified": True, "running_cancel_verified": True,
                              "recovery_verified": True, "audio_bytes": len(audio)}), flush=True)
        finally:
            await link.close()
            await node.close()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
