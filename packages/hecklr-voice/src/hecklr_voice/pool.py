"""The DSP process pool: utterance-sized audio work never runs on the event loop.

Synthesis, the impairment chain, codecs and model inference take milliseconds to seconds of CPU
per utterance. Run on the event loop, they would delay every other call's 20 ms frames (SC3), so
they go to anyio's worker processes, kept warm between calls.

At most half the cores run DSP at once. With every core busy, the OS stalls the event loop for
over 15 ms at a time (measured on a 10-core laptop), close to a whole frame period; with half,
under 1 ms. Even 50 calls need far less than half a modern machine for their DSP.
"""

import os
import time
from collections.abc import Callable
from typing import Unpack

import anyio
import anyio.to_process
from anyio import CapacityLimiter
from anyio.lowlevel import RunVar

_LIMITER: RunVar[CapacityLimiter] = RunVar("hecklr_voice_dsp_limiter")
_WARM: RunVar[bool] = RunVar("hecklr_voice_dsp_warm", default=False)


def dsp_workers() -> int:
    """How many DSP jobs may run at once: half the cores, at least one."""
    return max(1, (os.cpu_count() or 2) // 2)


def _limiter() -> CapacityLimiter:
    try:
        return _LIMITER.get()
    except LookupError:
        limiter = CapacityLimiter(dsp_workers())
        _LIMITER.set(limiter)
        return limiter


async def run_dsp[*Args, R](func: Callable[[Unpack[Args]], R], *args: *Args) -> R:
    """Run ``func(*args)`` in a worker process and return its result.

    ``func`` must be a module-level function and its arguments and result picklable. Cancelling
    the caller kills the worker mid-job rather than leaving it to finish unobserved. Programs
    using the voice engine must guard their entry point with ``if __name__ == "__main__":``,
    because worker processes import the parent's main module.
    """
    return await anyio.to_process.run_sync(func, *args, cancellable=True, limiter=_limiter())


def _ready() -> None:
    # Long enough that the warm-up jobs overlap, so each one starts its own worker.
    time.sleep(0.05)


async def warm_dsp_pool() -> None:
    """Start every DSP worker now (once per event loop; later calls return at once).

    Spawning a worker costs the parent tens of milliseconds of CPU on the event loop, so do it
    before calls start, not in the middle of one.
    """
    if _WARM.get():
        return
    _WARM.set(True)
    async with anyio.create_task_group() as group:
        for _ in range(dsp_workers()):
            group.start_soon(run_dsp, _ready)
