"""DeepSeek Harness 原生工具 → 内网绑定 B-py/A → CPU 引擎 → 宿主媒体交付。"""
from __future__ import annotations

import asyncio
import shutil
import socket
import wave
from pathlib import Path

import pytest
import uvicorn
from PIL import Image

from conftest import NODE_DIR, build_stack
from mmp_broker.http import create_app


@pytest.mark.skipif(shutil.which("node") is None or not (NODE_DIR.parent / "ts/packages/deepseek-harness/dist/index.js").exists(),
                    reason="需要 Node 与已构建的 @mmp/deepseek-harness")
async def test_native_harness_tools_on_bound_bpy(tmp_path):
    stack = await build_stack(tmp_path, engines={
        "media_image": {"module": "engines.media_image", "timeout_sec": 10},
        "media_audio": {"module": "engines.media_audio", "timeout_sec": 10},
    }, media__max_inline_result_bytes=16)
    image_path = tmp_path / "image.png"
    with Image.new("RGB", (12, 8), "red") as image:
        image.save(image_path)
    audio_path, large_path = tmp_path / "audio.wav", tmp_path / "large.wav"
    for path, frames in [(audio_path, 8000), (large_path, 2_100_000)]:
        with wave.open(str(path), "wb") as audio:
            audio.setparams((2, 2, 16000, 0, "NONE", ""))
            audio.writeframes(b"\x10\x00\x20\x00" * frames)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(stack.broker, "harness-integration-key"), log_level="error", lifespan="off"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if server.started:
                break
            if serving.done():
                await serving
            await asyncio.sleep(0.02)
        assert server.started
        process = await asyncio.create_subprocess_exec(
            shutil.which("node"), str(Path(__file__).with_name("deepseek_harness_host_smoke.mjs")),
            f"http://127.0.0.1:{port}", str(image_path), str(audio_path), str(large_path), str(tmp_path / "host"),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 90)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert b'"large_get":true' in stdout
        assert b'"redelivered":2' in stdout
    finally:
        server.should_exit = True
        await serving
        listener.close()
        await stack.client.aclose()
        await stack.link.close()
        await stack.node.close()
