"""Fail CI/deployment explicitly when the CPU media runtime is incomplete."""
from __future__ import annotations

import os
import re
import shutil
import subprocess

from PIL import Image, features


def main():
    if not all(features.check(name) for name in ("jpg", "zlib", "webp", "libtiff", "littlecms2", "freetype2")):
        raise RuntimeError("Pillow needs JPEG, PNG/zlib, WebP, TIFF, LittleCMS and FreeType support")
    Image.init()
    for fmt in ("PNG", "JPEG", "WEBP", "TIFF", "BMP"):
        if fmt not in Image.SAVE:
            raise RuntimeError(f"Pillow {fmt} encoder is unavailable")
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(os.environ.get(f"MMP_{tool.upper()}", tool))
        if path is None:
            raise RuntimeError(f"{tool} is unavailable; configure MMP_{tool.upper()}")
        result = subprocess.run([path, "-version"], capture_output=True, timeout=10)
        result.check_returncode()
        print(result.stdout.decode("utf-8", errors="replace").splitlines()[0])
        if tool == "ffmpeg":
            for flag, names in (("-encoders", {"pcm_s16le", "pcm_s24le", "flac", "libmp3lame", "libvorbis", "libopus", "aac"}),
                                ("-filters", {"aresample", "aformat", "pan", "volume", "alimiter", "ebur128", "afade",
                                              "apad", "adelay", "concat", "acrossfade", "amix", "aeval", "silencedetect", "astats"})):
                result = subprocess.run([path, "-hide_banner", flag], capture_output=True, timeout=10)
                result.check_returncode()
                available = {line.split()[1] for line in result.stdout.decode("utf-8").splitlines()
                             if re.match(r"^ [A-Z.]{3,6}\s+\S+", line)}
                if missing := names - available:
                    raise RuntimeError(f"FFmpeg {flag} missing: {', '.join(sorted(missing))}")
    print("CPU media dependencies ready")


if __name__ == "__main__":
    main()
