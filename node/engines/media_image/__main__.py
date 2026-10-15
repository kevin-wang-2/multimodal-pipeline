import asyncio
import threading
import time

from engines.common.io import BadArguments, serve
from engines.common.media_tools import MediaError
from engines.media_image import ENGINE, ENGINE_VERSION
from engines.media_image.runner import process

_slot = asyncio.Semaphore(1)


async def run(job, cancelled):
    async with _slot:
        started = time.monotonic()
        stop = threading.Event()
        worker = asyncio.create_task(asyncio.to_thread(process, job, stop))
        try:
            outputs = await asyncio.shield(worker)
        except asyncio.CancelledError:
            stop.set()
            # Do not release the memory slot while Pillow is still using it.
            await asyncio.gather(worker, return_exceptions=True)
            raise
        except MediaError as e:
            raise BadArguments(str(e)) from e
        return outputs, {"process": int((time.monotonic() - started) * 1000)}


if __name__ == "__main__":
    serve(ENGINE, ENGINE_VERSION, run)
