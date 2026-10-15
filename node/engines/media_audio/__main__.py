import asyncio
import time

from engines.common.io import BadArguments, serve
from engines.common.media_tools import MediaError
from engines.media_audio import ENGINE, ENGINE_VERSION
from engines.media_audio.runner import process

_slot = asyncio.Semaphore(1)


async def run(job, cancelled):
    async with _slot:
        started = time.monotonic()
        try:
            outputs = await process(job)
        except MediaError as e:
            raise BadArguments(str(e)) from e
        return outputs, {"process": int((time.monotonic() - started) * 1000)}


if __name__ == "__main__":
    serve(ENGINE, ENGINE_VERSION, run)
