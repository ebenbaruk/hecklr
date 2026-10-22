"""The voice adapter contract: every voice adapter must pass this suite against the loopback agent.

Covers connecting, caller audio reaching the agent at its rate, agent audio delivered as 16 kHz
real-time frames, SC4 (response latency and barge-in reaction measured by the real
``VoiceDriver`` within 20 ms of the agent's injected timing, over a real transport), agent
hang-up, agent failure, a new call after a failed one, a silent agent, clean hang-up, and an
event loop left free (spec "Agent adapters", "Calibration and self-tests"; WS10).
"""

import dataclasses
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
from hecklr_voice.audio import FRAME_MS, FRAME_SAMPLES, SAMPLE_RATE, speech_edges
from hecklr_voice.call import VoiceSettings
from hecklr_voice.driver import VoiceDriver
from hecklr_voice.testing import RecordingSink, ScriptedCaller, synthetic_reply, voice_trial

from .voice_cases import VOICE_CASES, VoiceCase
from .voice_livekit import LiveKitServer, docker_unavailable, livekit_server

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.enable_socket,
    pytest.mark.allow_hosts(["127.0.0.1"]),
    pytest.mark.usefixtures("frozen_heap"),
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


@pytest.fixture(scope="session")
def livekit() -> Iterator[LiveKitServer]:
    """One ``livekit-server`` per test process, or a skip saying what is missing."""
    reason = docker_unavailable()
    if reason is not None:
        pytest.skip(f"LiveKit contract needs Docker: {reason}")
    with livekit_server() as server:
        yield server


@pytest.fixture(params=VOICE_CASES, ids=lambda voice_case: voice_case.name)
def case(request: pytest.FixtureRequest) -> VoiceCase:
    """Every voice adapter configuration, each with its transport's reference agent."""
    voice_case: VoiceCase = request.param
    if voice_case.transport == "livekit":
        return dataclasses.replace(voice_case, environment=request.getfixturevalue("livekit"))
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


async def warm_up(case: VoiceCase) -> None:
    """Place and end one call first, so one-off start-up and teardown work (lazy imports in an
    agent framework) cannot land in the middle of the timed calls."""
    async with case.agent() as agent:
        link = await agent.target.connect(context())
        await link.hangup()
        await agent.wait_hung_up(await agent.call(0))
        await anyio.sleep(0.5)


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
    # Half a second of line silence first, as a caller listens before speaking: an agent may
    # not be listening the instant the call connects.
    clip = np.concatenate([np.zeros(8000, dtype=np.int16), synthetic_reply(SHORT_REPLY)])
    frames = len(clip) // FRAME_SAMPLES
    async with case.agent(reply_after_ms=10_000) as agent:
        link = await agent.target.connect(context())
        start = time.monotonic()
        for index in range(frames):
            due = start + index * 0.02
            await anyio.sleep(max(0.0, due - time.monotonic()))
            pcm = clip[index * FRAME_SAMPLES : (index + 1) * FRAME_SAMPLES]
            await link.send_frame(pcm.astype("<i2").tobytes(), SAMPLE_RATE)
        await anyio.sleep(0.6)  # the tail is still in flight through the agent's buffers
        call = await agent.call(0)
        await link.hangup()
    brain = call.brain
    assert brain.caller_rate == case.wire_rate
    assert brain.speech_ms, "the agent heard no caller speech"
    # Over WebRTC the reference agent times speech by arrival (its receiver stretches audio),
    # so the span it hears includes the transport's load-dependent jitter: judged only where
    # the case's timing is meaningful, like every other SC4 measurement (issue #28).
    if case.timing_is_meaningful:
        first, last = speech_edges(clip) or (0, 0)
        heard_ms = max(brain.speech_ms) - min(brain.speech_ms)
        assert abs(heard_ms - (last - first) / 16) <= TOLERANCE_MS + case.caller_loss_ms


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
    if case.timing_is_meaningful:
        speech = np.flatnonzero(np.abs(audio) > 300)
        speech_ms = (speech[-1] - speech[0]) / 16
        assert abs(speech_ms - len(reply) / 16) <= 60
        # Real time: the reply is heard over its own length, never in a burst.
        loud = [
            arrival
            for arrival, frame in received
            if np.abs(np.frombuffer(frame.pcm, "<i2")).max() > 300
        ]
        assert abs((loud[-1] - loud[0]) - len(reply) / 16) <= 80 + case.jitter_ms
    if case.lossless:
        transcript = await FakeASR().transcribe(audio.astype(np.int16), language="en")
        assert transcript.text == LONG_REPLY


@pytest.mark.sc4_timing
async def test_sc4_contract_response_latency_within_20ms_of_injected_delay(
    case: VoiceCase,
) -> None:
    """Measured latency = the agent's delay (as it actually spoke) + a constant transport delay."""
    case.require_idle_cpu_for_timing()
    offsets: list[float] = []

    async def measure(delay_ms: int) -> None:
        async with case.agent(reply=synthetic_reply(SHORT_REPLY), reply_after_ms=delay_ms) as agent:
            result, sink = await drive(agent.target)
            replies = (await agent.call(0)).brain.replies
        assert result.status == "ok", result
        latencies = [p.latency_ms for _, p in sink.utterances("agent")]
        assert len(latencies) == len(LINES) == len(replies), latencies
        for latency, reply in zip(latencies, replies, strict=True):
            assert latency is not None
            offsets.append(latency - delay_ms - reply.late_ms)

    await warm_up(case)
    async with anyio.create_task_group() as group:
        for delay_ms in (300, 600, 900):
            group.start_soon(measure, delay_ms)
    case.check_offsets(offsets, spread_ms=0.0)


@pytest.mark.sc4_timing
async def test_sc4_contract_barge_in_reaction_within_20ms_of_injected_reaction(
    case: VoiceCase,
) -> None:
    """Measured reaction = the agent's reaction (as it actually stopped) + transport delay."""
    case.require_idle_cpu_for_timing()
    offsets: list[float] = []

    async def measure(reaction_ms: int) -> None:
        speech = synthetic_reply(LONG_REPLY)
        async with case.agent(reply=speech, reply_after_ms=300, reaction_ms=reaction_ms) as agent:
            result, sink = await drive(
                agent.target, LINES[:2], behaviors={"interrupt_prob": 1.0}, max_turns=8
            )
            cut = [r for r in (await agent.call(0)).brain.replies if r.cut_ms is not None]
        assert result.status == "ok", result
        reactions = [p.reaction_ms for _, p in sink.interrupts() if p.style == "barge_in"]
        assert reactions
        assert len(reactions) == len(cut), (reactions, cut)
        for reaction, reply in zip(reactions, cut, strict=True):
            assert reaction is not None
            offsets.append(reaction - reaction_ms - reply.cut_late_ms)

    await warm_up(case)
    async with anyio.create_task_group() as group:
        for reaction_ms in (150, 300):
            group.start_soon(measure, reaction_ms)
    case.check_offsets(offsets, spread_ms=FRAME_MS if case.clears else 0.0)


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


async def test_ws10_contract_target_places_a_new_call_after_a_failed_one(case: VoiceCase) -> None:
    """A failed call leaves the target usable: the next trial connects and talks normally."""
    async with case.agent(
        reply=synthetic_reply(SHORT_REPLY),
        reply_after_ms=300,
        hang_up_after=1,
        fail_after_replies=1 if case.can_fail else None,
    ) as agent:
        first, _ = await drive(agent.target)
        second, sink = await drive(agent.target)
    assert first.status == ("agent_error" if case.can_fail else "ok"), first
    assert (second.status, second.end_reason) == ("ok", EndReason.AGENT_ENDED), second
    assert len(sink.utterances("agent")) == 1


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
