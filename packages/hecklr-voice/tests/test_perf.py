"""SC3 toward the benchmark: 50 simultaneous calls without frame underruns (in-process loopback).

The full SC3 benchmark runs the networked loopback agent (WS11); this checks the engine's own
share: 50 calls' pacing, receiving, segmenting and DSP in one event loop.
"""

import os

import anyio
import pytest

from hecklr_schemas import DriverResult, SessionContext, TrialId, new_id
from hecklr_voice.asr import FakeASR
from hecklr_voice.call import CallStats, VoiceSettings
from hecklr_voice.driver import VoiceDriver
from hecklr_voice.testing import (
    LoopbackVoiceTarget,
    RecordingSink,
    ScriptedCaller,
    synthetic_reply,
    voice_trial,
)


def context(timeout_s: float = 10.0) -> SessionContext:
    return SessionContext(trial_id=TrialId(new_id()), seed=3, timeout_s=timeout_s)


CALLS = 50
DELAY_MS = 500


@pytest.mark.perf
@pytest.mark.anyio
@pytest.mark.usefixtures("frozen_heap")
@pytest.mark.skipif((os.cpu_count() or 1) < 8, reason="SC3 is specified on an 8-core machine")
async def test_sc3_fifty_simultaneous_calls_without_underruns() -> None:
    reply = synthetic_reply("sure I can help with that")
    stats: list[CallStats] = []
    results: list[DriverResult] = []
    sinks: list[RecordingSink] = []

    async def call(index: int) -> None:
        voice = {"environment": "car_interior", "snr_db": 30} if index % 2 else None
        driver = VoiceDriver(
            LoopbackVoiceTarget(reply=reply, reply_after_ms=DELAY_MS),
            asr=FakeASR(),
            settings=VoiceSettings(greeting_wait_ms=200),
            on_call_end=stats.append,
        )
        sink = RecordingSink()
        lines = ["Hi, I need to move my visit.", "Thank you."]
        trial = voice_trial(seed=index, voice=voice)
        results.append(await driver.run(context(), trial, ScriptedCaller(lines), sink))
        sinks.append(sink)

    async with anyio.create_task_group() as group:
        for index in range(CALLS):
            group.start_soon(call, index)

    assert [result.status for result in results] == ["ok"] * CALLS
    assert sum(call.underruns for call in stats) == 0, max(c.max_lateness_ms for c in stats)
    latencies = [
        payload.latency_ms
        for sink in sinks
        for _, payload in sink.utterances("agent")
        if payload.latency_ms is not None
    ]
    worst = max(c.max_lateness_ms for c in stats)
    spread = f"{min(latencies)}-{max(latencies)} ms"
    print(f"SC3: {CALLS} calls, worst send lateness {worst:.1f} ms, latency {spread}")
    assert len(latencies) == 2 * CALLS
    assert all(abs(latency - DELAY_MS) <= 20 for latency in latencies), sorted(latencies)
