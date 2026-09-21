"""A 侧音频归一化：非 PCM WAV → ffmpeg → 16k 单声道 WAV。echo 是 modal=audio 的引擎，其 result.bytes 反映它实际拿到的文件。"""
from __future__ import annotations

import shutil
import subprocess
import wave

import pytest

from conftest import blob, build_stack, inline
from mmp_node.transcode import sniff_audio

from mmp_node.transcode import find_ffmpeg

FFMPEG = find_ffmpeg("ffmpeg")   # 找到但跑不起来（本机 brew 的 x265 缺失）也算没有


def test_sniff_audio_magic():
    assert sniff_audio(b"RIFF\x00\x00\x00\x00WAVEfmt ") == "wav"
    assert sniff_audio(b"\x00\x00\x00\x18ftypM4A \x00") == "m4a"
    assert sniff_audio(b"ID3\x03\x00") == "mp3" and sniff_audio(b"\xff\xfb\x90\x00") == "mp3"
    assert sniff_audio(b"OggS\x00") == "ogg" and sniff_audio(b"fLaC") == "flac"
    assert sniff_audio(b"#!AMR\n") == "amr"
    assert sniff_audio(b"%PDF-1.4") is None


def _make_m4a(path, seconds=1.0):
    subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
                    "-ac", "2", "-c:a", "aac", "-b:a", "64k", str(path)], check=True)


@pytest.mark.skipif(FFMPEG is None, reason="需要 ffmpeg")
async def test_m4a_is_transcoded_to_16k_mono_wav_for_audio_engines(tmp_path):
    m4a = tmp_path / "tone.m4a"
    _make_m4a(m4a)
    s = await build_stack(tmp_path)
    try:
        r = await s.submit(type="echo", media=inline(m4a.read_bytes()), wait=20)
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["timings_ms"]["transcode"] > 0
        expected = 16000 * 2 * 1 + 44                 # 1s @16k mono s16 + WAV 头
        assert abs(b["result"]["bytes"] - expected) < 2000, f"engine got {b['result']['bytes']} bytes, expected ≈ {expected} (16k mono wav)"
        derived = s.node.media.derived_path(b["media_id"], "16k.wav")
        with wave.open(str(derived)) as w:
            assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        # 第二次（不同 params 避开结果缓存）复用派生文件：transcode 0 ms
        r2 = await s.submit(type="echo", media={"ref": b["media_id"]}, params={"tag": "again"}, wait=20)
        assert r2.json()["timings_ms"]["transcode"] == 0
        # 认出是音频容器但解不开（m4a 魔数 + 垃圾）→ 400，说清是媒体的问题
        r3 = await s.submit(type="echo", media=inline(b"\x00\x00\x00\x18ftypM4A " + blob(500)), wait=20)
        assert r3.status_code == 400 and "undecodable" in r3.json()["message"]
        # 认不出的字节原样交给引擎，A 不拦
        r4 = await s.submit(type="echo", media=inline(blob(100)), wait=20)
        assert r4.status_code == 200 and "transcode" not in r4.json()["timings_ms"]
    finally:
        await s.client.aclose(); await s.link.close(); await s.node.close()


@pytest.mark.skipif(FFMPEG is None, reason="需要 ffmpeg")
async def test_real_wav_passes_through_untouched(tmp_path):
    import struct
    frames = b"".join(struct.pack("<h", 0) for _ in range(1600))
    wav = tmp_path / "s.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(frames)
    s = await build_stack(tmp_path)
    try:
        r = await s.submit(type="echo", media=inline(wav.read_bytes()), wait=20)
        assert r.status_code == 200 and "transcode" not in r.json()["timings_ms"]
        assert r.json()["result"]["bytes"] == wav.stat().st_size      # 8 kHz WAV 原样交给引擎，重采样是引擎的事
    finally:
        await s.client.aclose(); await s.link.close(); await s.node.close()
