"""The voice adapter contract: every voice adapter must pass this suite against the loopback agent.

Covers connecting, caller audio reaching the agent at its rate, agent audio delivered as 16 kHz
real-time frames, SC4 (response latency and barge-in reaction measured by the real
``VoiceDriver`` within 20 ms of the agent's injected timing, over a real transport), agent
hang-up, agent failure, a silent agent, clean hang-up, and an event loop left free
(spec "Agent adapters", "Calibration and self-tests"; WS10).
"""

import gc
import time
from collections.abc import Iterator, Sequence
from typing import Any

import anyio
import numpy as np
import pytest

from hecklr_adapters import AgentError
from hecklr_schemas import (
    AudioLink,
    DriverResult,
    EndReason,
    SessionContext,
    TimedFrame,
    TrialId,
    VoiceTarget,
    new_id,
)
from hecklr_voice.asr import FakeASR
from hecklr_voice.audio import FRAME_SAMPLES, SAMPLE_RATE
from hecklr_voice.call import VoiceSettings
from hecklr_voice.driver import VoiceDriver
from hecklr_voice.pool import warm_dsp_pool
from hecklr_voice.testing import RecordingSink, ScriptedCaller, synthetic_reply, voice_trial

from .voice_cases import VOICE_CASES, VoiceCase

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.enable_socket,
    pytest.mark.allow_hosts(["127.0.0.1"]),
    pytest.mark.usefixtures("frozen_heap", "warm_pool"),
]

SHORT_REPLY = "sure I can help with that"
LONG_REPLY = (
    "sure I can help with that let me look up your appointment and find a better slot for "
    "you next week in the morning while we are on the line"
)
LINES = ["Hi, I need to move my boiler visit.", "Next Tuesday morning works.", "Thanks!"]
FAST = VoiceSettings(greeting_wait_ms=200)
TOLERANCE_MS = 20


@pytest.fixture
def frozen_heap() -> Iterator[None]:
    """Keep pytest's garbage collections from pausing the loop mid-call (as in hecklr-voice)."""
    gc.collect()
    gc.freeze()
    yield
    gc.unfreeze()


@pytest.fixture
async def warm_pool() -> None:
    """Start the DSP worker processes before any call, as the driver does in a run."""
    await warm_dsp_pool()


@pytest.fixture(params=VOICE_CASES, ids=lambda voice_case: voice_case.name)
def case(request: pytest.FixtureRequest) -> VoiceCase:
    """Every voice adapter configuration, each with its transport's reference agent."""
    voice_case: VoiceCase = request.param
    voice_case.require()
    return voice_case


def context(timeout_s: float = 10.0) -> SessionContext:
    return SessionContext(trial_id=TrialId(new_id()), seed=3, timeout_s=timeout_s)


async def drive(
    target: VoiceTarget, lines: Sequence[str] = LINES, **trial: Any
) -> tuple[DriverResult, RecordingSink]:
    sink = RecordingSink()
    driver = VoiceDriver(target, asr=FakeASR(), settings=FAST)
    timeout_s = trial.pop("timeout_s", 10.0)
    result = await driver.run(context(timeout_s), voice_trial(**trial), ScriptedCaller(lines), sink)
    return result, sink


async def collect(link: AudioLink, limit_s: float) -> list[tuple[float, TimedFrame]]:
    frames: list[tuple[float, TimedFrame]] = []
    with anyio.move_on_after(limit_s):
        async for frame in link.frames():
            frames.append((time.monotonic_ns() / 1e6, frame))
    return frames


async def test_ws10_contract_target_and_link_satisfy_the_protocols(case: VoiceCase) -> None:
    async with case.agent(reply_after_ms=400) as agent:
        assert isinstance(agent.target, VoiceTarget)
        ctx = context()
        link = await agent.target.connect(ctx)
        assert isinstance(link, AudioLink)
        await link.send_frame(bytes(FRAME_SAMPLES * 2), SAMPLE_RATE)
        await link.hangup()
        await link.hangup()  # idempotent
        with pytest.raises(AgentError):
            await link.send_frame(bytes(FRAME_SAMPLES * 2), SAMPLE_RATE)
        call = await agent.call(0)
        assert call.trial_id == ctx.trial_id
        await agent.wait_hung_up(call)
        with pytest.raises(ValueError, match="16000"):
            await link.send_frame(bytes(160 * 2), 8000)


async def test_ws10_contract_caller_audio_reaches_the_agent_at_its_rate(case: VoiceCase) -> None:
    clip = synthetic_reply(SHORT_REPLY)
    frames = len(clip) // FRAME_SAMPLES
    async with case.agent(reply_after_ms=10_000) as agent:
        link = await agent.target.connect(context())
        start = time.monotonic()
        for index in range(frames):
            due = start + index * 0.02
            await anyio.sleep(max(0.0, due - time.monotonic()))
            pcm = clip[index * FRAME_SAMPLES : (index + 1) * FRAME_SAMPLES]
            await link.send_frame(pcm.astype("<i2").tobytes(), SAMPLE_RATE)
        await anyio.sleep(0.2)
        call = await agent.call(0)
        await link.hangup()
    brain = call.brain
    assert brain.caller_rate == case.wire_rate
    heard_ms = brain.caller_samples / (case.wire_rate / 1000)
    assert abs(heard_ms - frames * 20) <= 2 * 20
    assert brain.heard_speech


async def test_ws10_contract_agent_audio_arrives_as_real_time_16khz_frames(
    case: VoiceCase,
) -> None:
    reply = synthetic_reply(LONG_REPLY)
    async with case.agent(reply=reply, greeting=True, hang_up_after=1) as agent:
        link = await agent.target.connect(context())
        received = await collect(link, limit_s=15)
        await link.hangup()
    assert received, "no agent audio"
    assert all(frame.sample_rate == SAMPLE_RATE for _, frame in received)
    starts = [frame.sample_index for _, frame in received]
    ends = [frame.sample_index + len(frame.pcm) // 2 for _, frame in received]
    assert all(end <= start for end, start in zip(ends, starts[1:], strict=False)), "overlap"
    audio = np.concatenate([np.frombuffer(f.pcm, dtype="<i2") for _, f in received])
    speech = np.flatnonzero(np.abs(audio) > 300)
    speech_ms = (speech[-1] - speech[0]) / 16
    assert abs(speech_ms - len(reply) / 16) <= 60
    # Real time: the reply is heard over its own length, never in a burst.
    loud = [
        arrival
        for arrival, frame in received
        if np.abs(np.frombuffer(frame.pcm, "<i2")).max() > 300
    ]
    assert abs((loud[-1] - loud[0]) - len(reply) / 16) <= 80
    if case.lossless:
        assert received[0][1].pcm  # the tagged reply survives a lossless transport bit-exactly
        transcript = await FakeASR().transcribe(audio.astype(np.int16), language="en")
        assert transcript.text == LONG_REPLY


async def test_sc4_contract_response_latency_within_20ms_of_injected_delay(
    case: VoiceCase,
) -> None:
    results: dict[int, RecordingSink] = {}

    async def measure(delay_ms: int) -> None:
        async with case.agent(reply=synthetic_reply(SHORT_REPLY), reply_after_ms=delay_ms) as agent:
            result, sink = await drive(agent.target)
            assert result.status == "ok", result
            results[delay_ms] = sink

    async with anyio.create_task_group() as group:
        for delay_ms in (300, 600, 900):
            group.start_soon(measure, delay_ms)
    for delay_ms, sink in results.items():
        latencies = [p.latency_ms for _, p in sink.utterances("agent") if p.latency_ms is not None]
        assert len(latencies) == len(LINES), latencies
        bias = case.transport_delay_ms
        assert all(abs(lat - delay_ms - bias) <= TOLERANCE_MS for lat in latencies), latencies


async def test_sc4_contract_barge_in_reaction_within_20ms_of_injected_reaction(
    case: VoiceCase,
) -> None:
    results: dict[int, RecordingSink] = {}

    async def measure(reaction_ms: int) -> None:
        reply = synthetic_reply(LONG_REPLY)
        async with case.agent(reply=reply, reply_after_ms=300, reaction_ms=reaction_ms) as agent:
            result, sink = await drive(
                agent.target, LINES[:2], behaviors={"interrupt_prob": 1.0}, max_turns=8
            )
            assert result.status == "ok", result
            results[reaction_ms] = sink

    async with anyio.create_task_group() as group:
        for reaction_ms in (150, 300):
            group.start_soon(measure, reaction_ms)
    for reaction_ms, sink in results.items():
        barge_ins = [p for _, p in sink.interrupts() if p.style == "barge_in"]
        assert barge_ins
        reactions = [p.reaction_ms for p in barge_ins]
        low, high = case.reaction_window(reaction_ms)
        assert all(
            r is not None and low - TOLERANCE_MS <= r <= high + TOLERANCE_MS for r in reactions
        ), reactions


async def test_ws10_contract_agent_hang_up_ends_the_call(case: VoiceCase) -> None:
    async with case.agent(
        reply=synthetic_reply(SHORT_REPLY), reply_after_ms=300, hang_up_after=1
    ) as agent:
        result, sink = await drive(agent.target)
    assert (result.status, result.end_reason) == ("ok", EndReason.AGENT_ENDED)
    assert len(sink.utterances("agent")) == 1


async def test_ws10_contract_agent_failure_mid_call_is_an_agent_error(case: VoiceCase) -> None:
    if not case.can_fail:
        pytest.skip(f"{case.name}: the reference agent cannot fail abnormally on this transport")
    async with case.agent(
        reply=synthetic_reply(SHORT_REPLY), reply_after_ms=300, fail_after_replies=1
    ) as agent:
        result, _ = await drive(agent.target)
    assert result.status == "agent_error", result


async def test_ws10_contract_silent_agent_times_out(case: VoiceCase) -> None:
    async with case.agent(reply_after_ms=60_000) as agent:
        result, sink = await drive(agent.target, timeout_s=1.5)
    assert result.status == "timeout", result
    assert not sink.utterances("agent")


async def test_ws10_contract_adapter_leaves_the_event_loop_free(case: VoiceCase) -> None:
    """While a reply streams in, nothing holds the loop for 5 ms or more.

    A heartbeat sleeps 5 ms at a time. A wake-up that comes late because the loop thread was
    busy shows as both wall-clock lateness and loop-thread CPU time; lateness alone is the OS
    scheduling other processes, so the stall is the smaller of the two.
    """
    worst = 0.0

    async def heartbeat(stop: anyio.Event) -> None:
        nonlocal worst
        while not stop.is_set():
            cpu, wall = time.thread_time(), time.perf_counter()
            await anyio.sleep(0.005)
            late_ms = (time.perf_counter() - wall) * 1000 - 5
            worst = max(worst, min(late_ms, (time.thread_time() - cpu) * 1000))

    reply = synthetic_reply(LONG_REPLY)
    async with case.agent(reply=reply, greeting=True, hang_up_after=1) as agent:
        link = await agent.target.connect(context())
        stop = anyio.Event()
        async with anyio.create_task_group() as group:
            group.start_soon(heartbeat, stop)
            await collect(link, limit_s=10)
            stop.set()
        await link.hangup()
    assert worst < 5.0, worst
