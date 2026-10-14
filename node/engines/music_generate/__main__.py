import asyncio

from engines.common.io import BadArguments, serve
from engines.music_generate import ENGINE, ENGINE_VERSION
from engines.music_generate.runner import generate, prepare_backend, request


async def run(job: dict, cancelled: asyncio.Event):
    try:
        values = request(job)
    except ValueError as error:
        raise BadArguments(str(error)) from error
    outputs, timings, metrics = await asyncio.to_thread(generate, values, cancelled)
    return outputs, timings, None, metrics


if __name__ == "__main__":
    prepare_backend()
    serve(ENGINE, ENGINE_VERSION, run)
