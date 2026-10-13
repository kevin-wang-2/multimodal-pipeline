"""在独立 Python 环境中运行：python setup_env.py --root <ComfyUI 目录>。"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from engines.image_generate import COMFY_REVISION, MODEL_REVISION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if not (root / "main.py").is_file():
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "comfy.zip"
            urllib.request.urlretrieve(
                f"https://codeload.github.com/Comfy-Org/ComfyUI/zip/{COMFY_REVISION}", archive)
            with zipfile.ZipFile(archive) as zipped:
                zipped.extractall(tmp)
            root.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(Path(tmp) / f"ComfyUI-{COMFY_REVISION}"), root)
    subprocess.run([sys.executable, "-m", "pip", "install", "torch==2.14.1", "torchvision", "torchaudio",
                    "--index-url", "https://download.pytorch.org/whl/cu130"], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(root / "requirements.txt"),
                    "huggingface_hub"], check=True)
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    from huggingface_hub import snapshot_download
    snapshot_download("Comfy-Org/Krea-2", revision=MODEL_REVISION, local_dir=root / "models",
                      allow_patterns=["diffusion_models/krea2_turbo_fp8_scaled.safetensors",
                                      "text_encoders/qwen3vl_4b_fp8_scaled.safetensors",
                                      "vae/qwen_image_vae.safetensors"], max_workers=3)


if __name__ == "__main__":
    main()
