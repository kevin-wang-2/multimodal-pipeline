"""Install/verify the pinned YuE2 BF16 runtime and local model snapshots."""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlretrieve

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from engines.music_generate import EXPECTED_WEIGHTS, MODEL_REPOSITORY, MODEL_REVISION, VAE_REPOSITORY, VAE_REVISION

WHEEL = "yue2_infer-0.1.6-py3-none-any.whl"
WHEEL_SHA256 = "129a015327a2eca530760ffd2163ccb8cb65f7e601715de1d9626a40c568e2e2"


def install(root: Path) -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", "torch==2.10.0",
                    "--index-url", "https://download.pytorch.org/whl/cu128"], check=True)
    with tempfile.TemporaryDirectory() as temporary:
        wheel = Path(temporary) / WHEEL
        urlretrieve(f"https://github.com/multimodal-art-projection/YuE/releases/download/yue2-v0.1.6/{WHEEL}", wheel)
        with wheel.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != WHEEL_SHA256:
                raise RuntimeError("YuE2 release wheel integrity mismatch")
        subprocess.run([sys.executable, "-m", "pip", "install", str(wheel)], check=True)
    from huggingface_hub import snapshot_download
    from yue2.storage import MODEL_FILES

    for repo, revision, name in ((MODEL_REPOSITORY, MODEL_REVISION, "model"),
                                 (VAE_REPOSITORY, VAE_REVISION, "vae")):
        snapshot_download(repo, revision=revision, local_dir=root / name, max_workers=2,
                          allow_patterns=sorted(MODEL_FILES) + ["model-?????-of-?????.safetensors", "licenses/*"])


def verify(root: Path) -> None:
    from engines.music_generate.runner import prepare_backend

    _, _, Pipeline, _, _ = prepare_backend()
    with Pipeline.from_pretrained(str(root / "model"), vae=str(root / "vae"),
                                 device="cuda", local_files_only=True,
                                 backend="torch", quantization="none") as pipe:
        if pipe.weights != EXPECTED_WEIGHTS:
            raise RuntimeError("YuE2 local weights/config do not match the pinned revisions")
    print("YuE2 0.1.6 / torch 2.10.0+cu128 / BF16 cuDNN environment verified", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "verify"))
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "install":
        install(root)
    verify(root)


if __name__ == "__main__":
    main()
