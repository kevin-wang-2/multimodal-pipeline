"""Install the pinned Qwen Image 2.1 Turbo Int8 backend and only its required weights."""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from engines.qwen_image_internal import COMFY_REVISION, MODEL_REPOSITORY, MODEL_REVISION

MODEL_FILES = (
    "diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors",
    "text_encoders/qwen3vl_8b_int8_convrot.safetensors",
    "vae/qwen_image_2.1_vae_bf16.safetensors",
)
MODEL_SHA256 = {
    "diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors":
        "8144a0182964aa8403ad6dbc011db837304167d235161b9aabaa8532e967314c",
    "text_encoders/qwen3vl_8b_int8_convrot.safetensors":
        "8bfd0f6e12abf2d2d697ecc888e5e90b0d6741d6708f05799f53afa560452e8f",
    "vae/qwen_image_2.1_vae_bf16.safetensors":
        "bb21f7473051e1ac368515dd3f2e15cd44d7a11748ee8823e1ddca3e4876b7c9",
}
FORBIDDEN_FILES = (
    "diffusion_models/qwen_image_2.1_bf16.safetensors",
    "diffusion_models/qwen_image_2.1_int8_convrot.safetensors",
    "diffusion_models/qwen_image_2.1_turbo_bf16.safetensors",
)
PYTORCH_PACKAGES = (("torch", "2.14.1"), ("torchvision", "0.29.1"))


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".partial")
    subprocess.run([
        "curl.exe", "--fail", "--location", "--retry", "20", "--retry-delay", "5",
        "--retry-all-errors", "--continue-at", "-", "--output", str(partial), url,
    ], check=True)
    partial.replace(destination)


def install_comfy(root: Path) -> None:
    revision_file = root / ".mmp-comfy-revision"
    if (root / "main.py").is_file():
        installed = revision_file.read_text(encoding="utf-8").strip() if revision_file.is_file() else None
        if installed != COMFY_REVISION:
            raise RuntimeError(f"existing Comfy root has revision {installed!r}, expected {COMFY_REVISION}")
        return
    if root.exists() and any(root.iterdir()):
        raise RuntimeError(f"refusing to overwrite incomplete Comfy root: {root}")
    root.parent.mkdir(parents=True, exist_ok=True)
    archive = root.parent / f"ComfyUI-{COMFY_REVISION}.zip"
    download(f"https://codeload.github.com/Comfy-Org/ComfyUI/zip/{COMFY_REVISION}", archive)
    with tempfile.TemporaryDirectory(dir=root.parent) as temporary:
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(temporary)
        shutil.move(str(Path(temporary) / f"ComfyUI-{COMFY_REVISION}"), root)
    archive.unlink()
    revision_file.write_text(f"{COMFY_REVISION}\n", encoding="utf-8")


def install_dependencies(root: Path) -> None:
    wheels = root.parent / "wheels"
    python_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    wheel_paths = []
    for package, version in PYTORCH_PACKAGES:
        filename = f"{package}-{version}+cu130-{python_tag}-{python_tag}-win_amd64.whl"
        wheel = wheels / filename
        if not wheel.is_file():
            download(f"https://download.pytorch.org/whl/cu130/{quote(filename)}", wheel)
        wheel_paths.append(str(wheel))
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", *wheel_paths], check=True)
    pip_env = os.environ.copy()
    pip_env.update({"NO_PROXY": "*", "no_proxy": "*"})
    subprocess.run([
        sys.executable, "-m", "pip", "install", "-r", str(root / "requirements.txt"),
        "--index-url", "https://pypi.tuna.tsinghua.edu.cn/simple", "--timeout", "60", "--retries", "10",
    ], check=True, env=pip_env)
    shutil.rmtree(wheels)


def verify_dependencies() -> None:
    import torch
    import torchvision

    if torch.__version__ != "2.14.1+cu130":
        raise RuntimeError(f"expected torch 2.14.1+cu130, got {torch.__version__}")
    if torchvision.__version__ != "0.29.1+cu130":
        raise RuntimeError(f"expected torchvision 0.29.1+cu130, got {torchvision.__version__}")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def install_models(root: Path) -> None:
    models = root / "models"
    present = [name for name in MODEL_FILES if (models / name).is_file()]
    invalid = [name for name in present if sha256(models / name) != MODEL_SHA256[name]]
    if invalid:
        raise RuntimeError(f"installed model files do not match the pinned revision: {invalid}")
    missing = [name for name in MODEL_FILES if name not in present]
    if missing:
        free_gib = shutil.disk_usage(root).free / 1024**3
        if free_gib < 20:
            raise RuntimeError(f"only {free_gib:.1f} GiB free; need at least 20 GiB before model download")
        for name in missing:
            download(f"https://modelscope.cn/models/{MODEL_REPOSITORY}/resolve/master/{quote(name)}", models / name)
            digest = sha256(models / name)
            if digest != MODEL_SHA256[name]:
                raise RuntimeError(f"{name} has sha256 {digest}, expected {MODEL_SHA256[name]}")
    missing = [name for name in MODEL_FILES if not (models / name).is_file()]
    if missing:
        raise RuntimeError(f"required model files are missing: {missing}")
    unexpected = [name for name in FORBIDDEN_FILES if (models / name).exists()]
    if unexpected:
        raise RuntimeError(f"non-Turbo or BF16 diffusion models unexpectedly installed: {unexpected}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--skip-dependencies", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    install_comfy(root)
    verify_dependencies() if args.skip_dependencies else install_dependencies(root)
    install_models(root)
    print({
        "comfy_revision": COMFY_REVISION,
        "model_revision": MODEL_REVISION,
        "files": {name: (root / "models" / name).stat().st_size for name in MODEL_FILES},
    }, flush=True)


if __name__ == "__main__":
    main()
