"""The DSP process pool: utterance-sized audio work never runs on the event loop.

Synthesis, the impairment chain, codecs and model inference take milliseconds to seconds of CPU
per utterance. Run on the event loop, they would delay every other call's 20 ms frames (SC3), so
they go to anyio's worker processes (one per core by default, kept warm between calls).
"""

from collections.abc import Callable
from typing import Unpack

import anyio.to_process


async def run_dsp[*Args, R](func: Callable[[Unpack[Args]], R], *args: *Args) -> R:
    """Run ``func(*args)`` in a worker process and return its result.

    ``func`` must be a module-level function and its arguments and result picklable. Cancelling
    the caller kills the worker mid-job rather than leaving it to finish unobserved.
    """
    return await anyio.to_process.run_sync(func, *args, cancellable=True)
