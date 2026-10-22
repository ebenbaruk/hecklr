"""Calling the loopback agent with Hecklr's real voice stack, and scoring what Hecklr measured.

Every call goes through the harness exactly as ``hecklr run`` places it: ``VoiceDriver`` (caller
speech, frame pacing, agent segmentation, events) over the ``websocket`` adapter's ``pcm``
protocol to a loopback agent running in its own process. The agent's injected timings are the
truth; the latencies and barge-in reactions on the trial's events are what Hecklr measured.
"""

import os
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import anyio
import httpx2

from hecklr_adapters.voice import WebSocketVoiceAdapterConfig, WebSocketVoiceTarget
from hecklr_schemas import DriverResult, Estimate, SessionContext, TrialId, new_id
from hecklr_stats import quantile_estimates
from hecklr_voice import CallStats, FakeASR, VoiceDriver, VoiceSettings
from hecklr_voice.testing import RecordingSink, ScriptedCaller, voice_trial
from loopback_agent.call import LoopbackSettings

SERVE_SCRIPT: Final = Path(__file__).resolve().parents[1] / "serve.py"
CALLER_LINES: Final = ("Hi, I need to move my boiler visit please.", "Thank you, that is all.")
START_TIMEOUT_S: Final = 30.0


@dataclass(frozen=True, slots=True)
class CallMeasurement:
    """What Hecklr measured on one call."""

    result: DriverResult
    latencies_ms: tuple[int, ...]
    """``latency_ms`` of every agent turn that has one."""
    reactions_ms: tuple[int | None, ...]
    """``reaction_ms`` of every barge-in (``None``: the agent never stopped)."""
    stats: CallStats
    harness_errors: tuple[str, ...]


@dataclass(slots=True)
class LoopLag:
    """Worst lateness of a heartbeat on the harness event loop: how starved the loop was."""

    period_ms: float = 5.0
    worst_ms: float = 0.0
    beats: int = field(default=0)

    async def run(self) -> None:
        """Beat until cancelled."""
        while True:
            before = time.monotonic_ns()
            await anyio.sleep(self.period_ms / 1000)
            late = (time.monotonic_ns() - before) / 1e6 - self.period_ms
            self.worst_ms = max(self.worst_ms, late)
            self.beats += 1


async def place_call(
    url: str,
    *,
    seed: int,
    barge_in: bool,
    lines: Sequence[str] = CALLER_LINES,
    max_turns: int = 24,
    settings: VoiceSettings | None = None,
) -> CallMeasurement:
    """One scripted call to the agent at ``url``; with ``barge_in`` the caller talks over it."""
    stats: list[CallStats] = []
    driver = VoiceDriver(
        WebSocketVoiceTarget(WebSocketVoiceAdapterConfig(type="websocket", url=url)),
        asr=FakeASR(),
        settings=settings or VoiceSettings(greeting_wait_ms=300),
        on_call_end=stats.append,
    )
    sink = RecordingSink()
    behaviors: dict[str, object] = {"interrupt_prob": 1.0} if barge_in else {}
    trial = voice_trial(seed=seed, behaviors=behaviors, max_turns=max_turns)
    ctx = SessionContext(trial_id=TrialId(new_id()), seed=seed, timeout_s=15.0)
    result = await driver.run(ctx, trial, ScriptedCaller(list(lines)), sink)
    return CallMeasurement(
        result=result,
        latencies_ms=tuple(
            payload.latency_ms
            for _, payload in sink.utterances("agent")
            if payload.latency_ms is not None
        ),
        reactions_ms=tuple(
            payload.reaction_ms for _, payload in sink.interrupts() if payload.style == "barge_in"
        ),
        stats=stats[0] if stats else CallStats(0, 0, 0, 0.0, 0),
        harness_errors=tuple(sink.errors("harness")),
    )


async def place_calls(
    url: str, count: int, *, barge_in: bool, first_seed: int = 1
) -> list[CallMeasurement]:
    """``count`` calls at once."""
    results: list[CallMeasurement] = []

    async def one(seed: int) -> None:
        results.append(await place_call(url, seed=seed, barge_in=barge_in))

    async with anyio.create_task_group() as group:
        for seed in range(first_seed, first_seed + count):
            group.start_soon(one, seed)
    return results


def agent_arguments(settings: LoopbackSettings) -> list[str]:
    """Command-line options of ``serve.py`` for ``settings``."""
    reaction = settings.barge_in_reaction_ms
    arguments = [
        f"--reply-after-ms={settings.reply_after_ms}",
        f"--barge-in-reaction-ms={'none' if reaction is None else reaction}",
        f"--endpoint-ms={settings.endpoint_ms}",
        f"--reply-text={settings.reply_text}",
    ]
    return [*arguments, "--greeting"] if settings.greeting else arguments


@contextmanager
def agent_process(settings: LoopbackSettings) -> Iterator[str]:
    """Run the loopback agent in its own process on a free port; yield its ``ws://`` URL.

    A separate process is how an agent under test runs: its clock and its CPU are its own, so
    the harness's scheduling cannot move the truth it is measured against.
    """
    command = [sys.executable, str(SERVE_SCRIPT), "--port=0", *agent_arguments(settings)]
    environment = {**os.environ, "PYTHONUNBUFFERED": "1"}
    with subprocess.Popen(  # noqa: S603 -- our own script, with arguments we built
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment
    ) as process:
        try:
            yield _listening_url(process)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


def _listening_url(process: "subprocess.Popen[str]") -> str:
    if process.stdout is None:
        raise RuntimeError("the agent process has no stdout")
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if line.startswith("listening on "):
            return line.removeprefix("listening on ").strip()
        if not line and process.poll() is not None:
            errors = process.stderr.read() if process.stderr else ""
            raise RuntimeError(f"the loopback agent exited ({process.returncode}): {errors}")
    raise RuntimeError(f"the loopback agent did not start within {START_TIMEOUT_S} s")


def health(url: str) -> dict[str, object]:
    """The agent's ``/healthz`` report."""
    http_url = url.replace("ws://", "http://", 1).rstrip("/") + "/healthz"
    response = httpx2.get(http_url, timeout=5.0)
    response.raise_for_status()
    body: dict[str, object] = response.json()
    return body


@dataclass(frozen=True, slots=True)
class ErrorSummary:
    """How far Hecklr's measurements were from the truth, over many calls."""

    count: int
    """Measurements (turns or barge-ins)."""
    calls: int
    missing: int
    """Expected measurements Hecklr did not make (a barge-in it saw no reaction to)."""
    worst_ms: float
    """Largest absolute error."""
    bias_ms: Estimate
    """Median signed error (measured minus true), with a bootstrap interval over calls."""
    p95_ms: Estimate
    """95th percentile of the absolute error, with a bootstrap interval over calls."""

    def within(self, tolerance_ms: float) -> bool:
        """Whether every measurement was made and within ``tolerance_ms`` of the truth."""
        return self.missing == 0 and self.worst_ms <= tolerance_ms


def error_summary(
    measured_by_call: Sequence[Sequence[float | None]], truth_ms: float, *, seed: int
) -> ErrorSummary:
    """Summarize ``measured - truth`` per call.

    Turns of one call share its clock and transport, so the bootstrap resamples whole calls.
    """
    signed: list[float] = []
    groups: list[int] = []
    missing = 0
    for call, values in enumerate(measured_by_call):
        for value in values:
            if value is None:
                missing += 1
                continue
            signed.append(value - truth_ms)
            groups.append(call)
    if not signed:
        raise ValueError("no measurement to summarize")
    absolute = [abs(error) for error in signed]
    (bias,) = quantile_estimates(signed, [0.5], seed=seed, resamples=2_000, groups=groups)
    (p95,) = quantile_estimates(absolute, [0.95], seed=seed, resamples=2_000, groups=groups)
    return ErrorSummary(
        count=len(signed),
        calls=len(set(groups)),
        missing=missing,
        worst_ms=max(absolute),
        bias_ms=bias,
        p95_ms=p95,
    )


def cpu_seconds(pids: Sequence[int]) -> float:
    """User plus system CPU time of live processes, as ``ps`` reports it (macOS and Linux)."""
    if not pids:
        return 0.0
    listing = subprocess.run(  # noqa: S603 -- fixed command, integer arguments
        ["/bin/ps", "-o", "time=", "-p", ",".join(str(pid) for pid in pids)],
        capture_output=True,
        text=True,
        check=False,
    )
    return sum(_clock_seconds(line.strip()) for line in listing.stdout.splitlines() if line.strip())


def child_pids() -> list[int]:
    """Live child processes of this process (the DSP workers)."""
    listing = subprocess.run(
        ["/bin/ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, check=False
    )
    me = os.getpid()
    pids = []
    for line in listing.stdout.splitlines():
        pid, _, ppid = line.strip().partition(" ")
        if ppid.strip() == str(me):
            pids.append(int(pid))
    return pids


def _clock_seconds(text: str) -> float:
    """``[[dd-]hh:]mm:ss[.ss]`` as seconds."""
    days, _, clock = text.rpartition("-")
    seconds = 0.0
    for part in clock.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds + (int(days) * 86_400 if days else 0)
