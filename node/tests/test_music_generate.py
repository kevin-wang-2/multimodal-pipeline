import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from conftest import build_stack
from engines.music_generate import ENGINE, ENGINE_VERSION
from engines.music_generate.runner import request
from mmp_node.engine_io import EngineProcess, EngineResult
from mmp_node.registry import EngineSpec


@pytest.mark.parametrize("arguments", [
    {"style": " ", "lyrics": "[Verse]\nHello"},
    {"style": "pop", "lyrics": ""},
    {"style": "pop", "lyrics": "Hello", "options": {"seed": True}},
    {"style": "pop", "lyrics": "Hello", "score": " "},
])
def test_invalid_inputs_fail_before_loading(arguments):
    with pytest.raises(ValueError):
        request({"arguments": arguments})


async def test_music_queue_score_reuse_delivery_and_cache(monkeypatch, tmp_path, media_server):
    events = []
    active = 0

    class FakeProcess:
        def __init__(self, spec, *args):
            self.alive, self.busy, self.last_used = False, 0, time.monotonic()

        async def start(self):
            self.alive = True

        async def run(self, job, timeout_sec):
            nonlocal active
            active += 1
            assert active == 1, "music jobs must not overlap on the GPU"
            values = request(job)
            events.append(values)
            await asyncio.sleep(.02)
            active -= 1
            return EngineResult({
                "audio": {"kind": "media", "content_type": "audio/flac", "data_base64": "AAAA" * 2000},
                "score": {"kind": "text", "text": values.get("abc", "X:1\nK:C\nC D E F")},
                "metadata": {"kind": "json", "value": {
                    "sample_rate": 48000, "channels": 2, "duration_sec": 60, "seed": values["seed"],
                    "truncated": {"abc": False, "semantic": False}, "attention": "cudnn",
                    "ar_execution": "cuda_graph", "model_dtype": "bfloat16", "vae_dtype": "float32",
                    "ode_steps": 32,
                }},
            }, {}, compute_ms=20)

        async def shutdown(self):
            self.alive = False

    monkeypatch.setattr("mmp_node.engine_io.EngineProcess", FakeProcess)
    stack = await build_stack(tmp_path, engines={"music_generate": {
        "module": "engines.music_generate", "timeout_sec": 900}}, scheduler__vram_total_mb=16303)
    try:
        body = {"type": "music.generate", "arguments": {"style": "piano pop", "lyrics": "[Verse]\nHello"},
                "delivery": {"audio": media_server.put_endpoint("/song.flac")}}
        first, second = await asyncio.gather(stack.submit(**body), stack.submit(**{
            **body, "arguments": {**body["arguments"], "options": {"seed": 42}},
            "delivery": {"audio": media_server.put_endpoint("/other.flac")}}))
        done = await stack.finish(first.json()["job_id"])
        await stack.finish(second.json()["job_id"])
        assert done["status"] == "done", done
        assert done["source"]["engine"] == ENGINE
        assert done["source"]["engine_version"] == ENGINE_VERSION
        assert done["outputs"]["audio"]["delivered"] is True
        cached = (await stack.submit(**{**body, "wait": 5})).json()
        assert cached["cached"] and cached["usage"]["compute_ms"] == 0
        assert len(events) == 2
        score = done["outputs"]["score"]["text"].replace("C D E F", "G A B c")
        revised = (await stack.submit(**{**body, "arguments": {**body["arguments"], "score": score},
                                         "wait": 5})).json()
        assert revised["status"] == "done" and not revised["cached"]
        assert events[-1]["abc"] == score
        assert revised["outputs"]["score"]["text"] == score
        assert stack.node.pool.loaded() == []
    finally:
        await stack.client.aclose()
        await stack.link.close()
        await stack.node.close()


async def test_full_song_frame_can_exceed_64_mib(tmp_path):
    script = tmp_path / "large_audio.py"
    script.write_text(
        "import json,sys\n"
        "print(json.dumps({'type':'hello','engine':'large','engine_version':'1','protocol_version':'3.0'}),flush=True)\n"
        "for line in sys.stdin:\n"
        " m=json.loads(line)\n"
        " if m['type']=='shutdown': break\n"
        " if m['type']=='run':\n"
        "  print(json.dumps({'type':'result','id':m['id'],'outputs':{'audio':{'kind':'media','content_type':'audio/flac','data_base64':'A'*(65*1024*1024)}}}),flush=True)\n")
    proc = EngineProcess(EngineSpec("large", "large", [sys.executable, str(script)], tmp_path, 30, []), 30, "3.0")
    try:
        await proc.start()
        result = await proc.run({"job_id": "large-song", "task_type": "music.generate", "tier": "gpu",
                                 "arguments": {"style": "pop", "lyrics": "Hello"}}, 30)
        assert len(result.outputs["audio"]["data_base64"]) == 65 * 1024 * 1024
    finally:
        await proc.shutdown()
