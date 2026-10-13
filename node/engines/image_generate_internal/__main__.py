from engines.common.io import serve
from engines.image_generate.__main__ import run
from engines.image_generate.runner import prepare_backend
from engines.image_generate_internal import ENGINE, ENGINE_VERSION


if __name__ == "__main__":
    prepare_backend()
    serve(ENGINE, ENGINE_VERSION, run)
