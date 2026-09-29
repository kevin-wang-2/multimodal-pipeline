import os

from engines.triage_image import ENGINE, ENGINE_VERSION
from engines.triage_image.runner import main

if __name__ == "__main__":
    main(ENGINE, ENGINE_VERSION,
         os.environ.get("MMP_MODELS_DIR", "models") + "/Florence-2-base-ft-native", True)
