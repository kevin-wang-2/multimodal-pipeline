import asyncio
import base64
import json
import sys
import time
from pathlib import Path

import pytest

from conftest import build_stack, inline
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engines.image_generate import ENGINE, ENGINE_VERSION
from engines.image_generate.runner import request
from mmp_node.engine_io import EngineProcess, EngineResult
from mmp_node.registry import EngineSpec


async def test_generation_uses_queue_put_and_cache_without_resident_process(monkeypatch, tmp_path, media_server):
    events = []

    class FakeProcess:
        def __init__(self, spec, *args):
            self.alive, self.busy, self.last_used = False, 0, time.monotonic()

        async def start(self):
            self.alive = True
            events.append("start")

        async def run(self, job, timeout_sec):
            prompt, params = Path(job["media_path"]).read_text(), job["params"]
            assert prompt == "A hand painted game village"
            events.append("generate")
            return EngineResult({"content_type": "image/png", "data_base64": "AAAA" * 2000,
                                 **params}, {})

        async def shutdown(self):
            self.alive = False
            events.append("stop")

    monkeypatch.setattr("mmp_node.engine_io.EngineProcess", FakeProcess)
    stack = await build_stack(tmp_path, engines={"image_generate": {
        "module": "engines.image_generate", "timeout_sec": 30}}, scheduler__vram_total_mb=16303)
    try:
        media = {**inline(b"A hand painted game village"), "content_type": "text/plain",
                 "put": media_server.put_endpoint("/image.json")}
        done = (await stack.submit(type="image.generate", media=media, wait=5)).json()
        assert done["status"] == "done", done
        assert done["source"]["engine"] == ENGINE
        assert done["source"]["engine_version"] == ENGINE_VERSION
        assert done["result_ref"]["content_type"] == "application/json"
        image = json.loads(media_server.puts["/image.json"])
        assert image["width"] == image["height"] == 1024 and image["seed"] == 42
        assert events == ["start", "generate", "stop"]
        assert stack.node.pool.loaded() == []
        cached = (await stack.submit(type="image.generate", media={**media,
            "put": media_server.put_endpoint("/second-image.json")}, wait=5)).json()
        assert cached["cached"] is True and cached["usage"]["compute_ms"] == 0
        assert json.loads(media_server.puts["/second-image.json"]) == image
        assert events == ["start", "generate", "stop"]
    finally:
        await stack.client.aclose()
        await stack.link.close()
        await stack.node.close()


@pytest.mark.parametrize("raw,params", [(b"\xff", {}), (b" ", {}),
                                       (b"village", {"width": 1536, "height": 1536})])
def test_invalid_prompt_fails_before_model_loading(tmp_path, raw, params):
    path = tmp_path / "prompt.txt"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        request({"media_path": str(path), "params": params})


@pytest.mark.skipif(sys.platform != "win32", reason="Windows venv process tree")
async def test_forced_engine_kill_also_terminates_child(tmp_path):
    import ctypes
    script = tmp_path / "engine.py"
    pid_file = tmp_path / "child.pid"
    script.write_text("import subprocess, sys, time, json\n"
                      "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                      f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
                      "print(json.dumps({'type':'hello','engine':'tree','engine_version':'1','protocol_version':'2.0'}), flush=True)\n"
                      "time.sleep(120)\n")
    process = EngineProcess(EngineSpec("tree", "tree", [sys.executable, str(script)], tmp_path, 5, []), 5, "2.0")
    await process.start()
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x100000, False, int(pid_file.read_text()))
    assert handle
    try:
        await process.kill()
        assert kernel.WaitForSingleObject(handle, 5000) == 0
    finally:
        kernel.CloseHandle(handle)
        await process.kill()
