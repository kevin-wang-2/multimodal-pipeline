"""音频归一化：不是 PCM WAV 的音频（m4a / aac / mp3 / ogg / opus / flac / amr …）在 A 侧用 ffmpeg 转成 16 kHz 单声道 WAV，再交给引擎。

放在 A 而不是引擎里：所有声明 audio.to_wav_16k_mono 的任务共用；引擎环境不必装 ffmpeg；media_id 仍是原始字节的 hash，缓存键不受影响。
转出的文件是原文件的派生物，跟原文件一起被 LRU 淘汰。
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path

from .errors import ApiError

log = logging.getLogger("mmp.transcode")

TARGET_SR = 16000


def sniff_audio(head: bytes) -> str | None:
    """按魔数判音频容器。None = 不认识（交给引擎自己判）。"""
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    if head[4:8] == b"ftyp":                      # m4a / mp4 / 3gp
        return "m4a"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:6] == b"#!AMR\n" or head[:9] == b"#!AMR-WB\n":
        return "amr"
    if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return "aiff"
    if head[:4] == b"\x1aE\xdf\xa3":              # webm / mkv
        return "webm"
    return None


def needs_transcode(path: Path) -> bool:
    """只对魔数认出的非 WAV 音频容器转码；PCM WAV 与认不出的字节原样交给引擎（A 不替引擎拒绝它不认识的东西）。"""
    with open(path, "rb") as f:
        kind = sniff_audio(f.read(16))
    return kind is not None and kind != "wav"


_ffmpeg_cache: dict[str, str | None] = {}


def find_ffmpeg(configured: str) -> str | None:
    """找到且能运行（`-version` 返回 0）才算有：坏掉的动态库会让 ffmpeg 启动即 abort，得当作没有。"""
    if configured in _ffmpeg_cache:
        return _ffmpeg_cache[configured]
    path = shutil.which(configured) if configured else None
    if path is not None:
        try:
            ok = subprocess.run([path, "-version"], capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if not ok:
            log.warning("ffmpeg at %s does not run; audio transcoding disabled on this node", path)
            path = None
    _ffmpeg_cache[configured] = path
    return path


async def to_wav16k(src: Path, dst: Path, ffmpeg: str, timeout_sec: float) -> None:
    """ffmpeg 转码；失败抛 ApiError(bad_request)：这是调用方给的媒体不可解码，不是服务故障。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".part.wav")
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
           "-vn", "-ac", "1", "-ar", str(TARGET_SR), "-sample_fmt", "s16", "-f", "wav", str(tmp)]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        tmp.unlink(missing_ok=True)
        raise ApiError("bad_request", f"audio transcode exceeded {timeout_sec}s")
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size <= 44:
        tmp.unlink(missing_ok=True)
        msg = (err or b"").decode(errors="replace").strip().splitlines()
        raise ApiError("bad_request", f"undecodable audio: {msg[-1] if msg else f'ffmpeg rc={proc.returncode}'}")
    tmp.replace(dst)
