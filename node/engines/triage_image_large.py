"""triage.image 的内部一次性质量兜底；不注册为对外 capability。"""
import os

from engines.triage_image import LARGE_ENGINE, LARGE_ENGINE_VERSION
from engines.triage_image.runner import main

CAPABILITIES = []
INTERNAL_SOURCE = {"tier": "gpu", "engine": LARGE_ENGINE, "engine_version": LARGE_ENGINE_VERSION}

if __name__ == "__main__":
    main(LARGE_ENGINE, LARGE_ENGINE_VERSION,
         os.environ.get("MMP_MODELS_DIR", "models") + "/Florence-2-large-ft-native", False)
