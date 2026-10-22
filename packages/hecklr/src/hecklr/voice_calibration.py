"""SC4 voice timing calibration: measure a loopback agent whose timing is known (issue #24).

The loopback agent (``hecklr_voice.testing.LoopbackVoiceTarget``) answers every caller utterance
exactly ``reply_after_ms`` after the caller's last speech sample, and stops talking exactly
``barge_in_reaction_ms`` after the caller starts speaking over it. Calls go through the real
``VoiceDriver``, so what is checked is what ``hecklr run`` measures on every voice trial:
response latency and barge-in reaction must be within 20 ms of the injected truth.
"""

import gc
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import anyio
import numpy as np

from hecklr.channels import VOICE_EXTRA, ChannelUnavailableError
from hecklr_schemas import (
    Estimate,
    IntervalMethod,
    SessionContext,
    TrialId,
    new_id,
)
from hecklr_stats.intervals import DEFAULT_CONFIDENCE, percentile_interval

INJECTED_DELAYS_MS: Final = (300, 600, 900)
"""Reply delays the spec names for the loopback agent."""
INJECTED_REACTIONS_MS: Final = (150, 300)
SC4_BOUND_MS: Final = 20.0
CALLS_PER_SETTING: Final = 2
LINES: Final = ("Hi, I need to move my boiler visit.", "Next Tuesday morning works.", "Thanks!")
SHORT_REPLY: Final = "sure I can help with that"
LONG_REPLY: Final = (
    "sure I can help with that let me look up your appointment and find a better slot for "
    "you next week in the morning while we are on the line"
)
_RESAMPLES: Final = 10_000


@dataclass(frozen=True, slots=True)
class TimingCheck:
    """Measurements of one timing metric against one injected value."""

    metric: str
    """``response_latency`` or ``barge_in_reaction``."""
    injected_ms: int
    errors_ms: tuple[float, ...]
    """Measured minus injected, one per measurement."""

    @property
    def worst_ms(self) -> float:
        """The largest absolute error."""
        return max(abs(error) for error in self.errors_ms)

    @property
    def passed(self) -> bool:
        """Every measurement within the SC4 bound (and at least one measurement)."""
        return bool(self.errors_ms) and self.worst_ms <= SC4_BOUND_MS

    def mean_error(self, seed: int) -> Estimate:
        """Mean error with a seeded bootstrap percentile interval."""
        errors = np.asarray(self.errors_ms, dtype=float)
        rng = np.random.default_rng(seed)
        means = rng.choice(errors, size=(_RESAMPLES, errors.size)).mean(axis=1)
        low, high = percentile_interval(means, DEFAULT_CONFIDENCE)
        value = float(errors.mean())
        return Estimate(
            value=value,
            ci_low=min(low, value),
            ci_high=max(high, value),
            confidence=DEFAULT_CONFIDENCE,
            n=int(errors.size),
            method=IntervalMethod.BOOTSTRAP,
        )


async def run_voice_loopback(seed: int = 0) -> list[TimingCheck]:
    """Place the calibration calls (concurrently, in real time) and return every check.

    Raises:
        ChannelUnavailableError: ``hecklr-voice`` is not installed.
    """
    try:
        # Imported here: hecklr-voice is an optional extra.
        from hecklr_voice import FakeASR, VoiceDriver, VoiceSettings
        from hecklr_voice.testing import (
            LoopbackVoiceTarget,
            RecordingSink,
            ScriptedCaller,
            synthetic_reply,
            voice_trial,
        )
    except ImportError as exc:
        raise ChannelUnavailableError(
            f"--voice-loopback needs the voice engine: {VOICE_EXTRA}"
        ) from exc

    # A full collection over the heap pauses the loop for tens of ms mid-call (ADR 0009a).
    gc.collect()
    gc.freeze()
    settings = VoiceSettings(greeting_wait_ms=200)
    latencies: dict[int, list[float]] = {delay: [] for delay in INJECTED_DELAYS_MS}
    reactions: dict[int, list[float]] = {reaction: [] for reaction in INJECTED_REACTIONS_MS}

    async def call(target: LoopbackVoiceTarget, barge_in: bool, index: int) -> RecordingSink:
        sink = RecordingSink()
        trial = voice_trial(
            behaviors={"interrupt_prob": 1.0} if barge_in else None,
            max_turns=8 if barge_in else 24,
            seed=seed + index,
        )
        ctx = SessionContext(trial_id=TrialId(new_id()), seed=seed + index, timeout_s=10.0)
        driver = VoiceDriver(target, asr=FakeASR(), settings=settings)
        await driver.run(ctx, trial, ScriptedCaller(LINES[:2] if barge_in else LINES), sink)
        return sink

    async def measure_delay(delay: int, index: int) -> None:
        target = LoopbackVoiceTarget(reply=synthetic_reply(SHORT_REPLY), reply_after_ms=delay)
        sink = await call(target, False, index)
        latencies[delay] += [
            p.latency_ms - delay for _, p in sink.utterances("agent") if p.latency_ms is not None
        ]

    async def measure_reaction(reaction: int, index: int) -> None:
        target = LoopbackVoiceTarget(
            reply=synthetic_reply(LONG_REPLY), reply_after_ms=300, barge_in_reaction_ms=reaction
        )
        sink = await call(target, True, index)
        reactions[reaction] += [
            p.reaction_ms - reaction
            for _, p in sink.interrupts()
            if p.style == "barge_in" and p.reaction_ms is not None
        ]

    try:
        async with anyio.create_task_group() as calls:
            for index in range(CALLS_PER_SETTING):
                for delay in INJECTED_DELAYS_MS:
                    calls.start_soon(measure_delay, delay, index)
                for reaction in INJECTED_REACTIONS_MS:
                    calls.start_soon(measure_reaction, reaction, index)
    finally:
        gc.unfreeze()
    return _checks("response_latency", latencies) + _checks("barge_in_reaction", reactions)


def _checks(metric: str, errors: dict[int, list[float]]) -> list[TimingCheck]:
    return [TimingCheck(metric, injected, tuple(found)) for injected, found in errors.items()]


def all_passed(checks: Sequence[TimingCheck]) -> bool:
    """Whether the calibration holds: every check measured something, all within the bound."""
    return bool(checks) and all(check.passed for check in checks)
