"""普通版后端安装完成后，在同一独立环境运行此脚本下载内部版模型权重。"""
import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from engines.image_generate_internal import MODEL_REPOSITORY, MODEL_REVISION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if not (root / "main.py").is_file():
        raise SystemExit("install the Comfy backend first")
    from huggingface_hub import snapshot_download
    staging = root / "models" / ".internal-download"
    paths = {
        "moodyKrea2Mix_v3-Global-FP8.safetensors": "diffusion_models",
        "qwen3VL4BAbliteratedComfyui_FP8.safetensors": "text_encoders",
    }
    snapshot_download(MODEL_REPOSITORY, revision=MODEL_REVISION, local_dir=staging,
                      allow_patterns=list(paths))
    for name, directory in paths.items():
        destination = root / "models" / directory / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging / name), destination)


if __name__ == "__main__":
    main()
