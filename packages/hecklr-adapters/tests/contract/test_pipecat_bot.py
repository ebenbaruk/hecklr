"""Why a reply from the Pipecat SmallWebRTC reference bot can be heard late (issue #27).

The bot runs on a thread of the test process. When that thread is starved of CPU (or of the GIL),
Pipecat's output track stops sending; when it runs again it sends every overdue frame at once
to catch up with its clock. The caller's playout, like a phone that never drops audio, plays
the burst back to back. Pipecat fills quiet time with silence frames, so the line never goes
quiet and the added delay lasts for the rest of the call. The SmallWebRTC case's ``jitter_ms``
allowance covers this (ADR 00hfa).
"""

import time
import warnings
from collections.abc import Awaitable, Callable

import anyio
import numpy as np
import pytest

from hecklr_adapters.voice.audio import FRAME_SAMPLES
from hecklr_adapters.voice.playout import Playout

with warnings.catch_warnings():
    # Pipecat's audio helpers still import the stdlib ``audioop``, deprecated in Python 3.12.
    warnings.filterwarnings("ignore", "'audioop' is deprecated", DeprecationWarning)
    from pipecat.transports.smallwebrtc.transport import RawAudioTrack


@pytest.mark.anyio
async def test_issue27_pipecats_output_track_sends_overdue_frames_in_a_burst() -> None:
    track = RawAudioTrack(sample_rate=48_000)
    next_frame: Callable[[], Awaitable[object]] = track.recv  # Pipecat leaves it unannotated
    await next_frame()  # starts the track's clock
    await anyio.sleep(0.2)  # the bot's thread does not run for 200 ms
    started = time.monotonic()
    for _ in range(19):  # the 10 ms frames that fell due meanwhile
        await next_frame()
    assert time.monotonic() - started < 0.1  # paced, they would take 190 ms


def test_issue27_a_burst_on_a_line_that_never_goes_quiet_delays_the_rest_of_the_call() -> None:
    clock = 0.0
    playout = Playout(now_ms=lambda: clock)
    heard_at: dict[int, float] = {}  # frame number -> when its audio is heard (playout time)

    def arrive(number: int) -> None:
        playout.write(np.full(FRAME_SAMPLES, number, np.int16))

    def listen_until(until_ms: float) -> None:
        nonlocal clock
        while clock < until_ms:
            while (frame := playout.pop_frame()) is not None:
                heard_at[int(np.frombuffer(frame.pcm, "<i2")[0])] = frame.sample_index / 16
            clock += 1.0

    for number in range(5):  # frames 0-4 on time, 20 ms apart
        listen_until(number * 20)
        arrive(number)
    listen_until(300)  # the sender stalls for 200 ms, then catches up: frames 5-15 at once
    for number in range(5, 16):
        arrive(number)
    for number in range(16, 30):  # and then sends on time again
        listen_until(number * 20)
        arrive(number)
    listen_until(1000)
    assert heard_at[4] == 80
    assert heard_at[29] == 29 * 20 + 200
