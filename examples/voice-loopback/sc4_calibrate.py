"""SC4 calibration over a real network transport.

For each injected response delay (300, 600, 900 ms, each with a barge-in reaction), start the
loopback agent in its own process, place calls through Hecklr's ``VoiceDriver`` and ``websocket``
adapter, and compare every measured latency and barge-in reaction with the injected value::

    uv run python examples/voice-loopback/sc4_calibrate.py --calls 10 --json sc4.json

Exits 1 when any measurement is more than ``--tolerance-ms`` (default 20) from the truth.
"""

import argparse
import gc
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

import anyio
from loopback_agent.bench import (
    CallMeasurement,
    ErrorSummary,
    agent_process,
    error_summary,
    health,
    json_default,
    place_calls,
)
from loopback_agent.call import LoopbackSettings

CASES: Final = ((300.0, 150.0), (600.0, 300.0), (900.0, 200.0))
"""(response delay, barge-in reaction) pairs, in ms."""
BATCH: Final = 5
"""Calls placed at once: calibration measures accuracy, not load."""


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One injected delay and reaction, and how well Hecklr measured them."""

    delay_ms: float
    reaction_ms: float
    calls: int
    statuses: list[str]
    underruns: int
    latency: ErrorSummary
    reaction: ErrorSummary
    agent: object
    """The agent's own ``/healthz`` stats: replies, cuts, its worst send lateness."""

    def within(self, tolerance_ms: float) -> bool:
        """Whether SC4 holds for this case."""
        return self.latency.within(tolerance_ms) and self.reaction.within(tolerance_ms)


async def _calls(url: str, count: int, *, barge_in: bool, first_seed: int) -> list[CallMeasurement]:
    results: list[CallMeasurement] = []
    for start in range(0, count, BATCH):
        size = min(BATCH, count - start)
        results += await place_calls(url, size, barge_in=barge_in, first_seed=first_seed + start)
    return results


async def calibrate_case(delay_ms: float, reaction_ms: float, calls: int) -> CaseResult:
    """Measure one injected delay and reaction over ``calls`` plain and ``calls`` barge-in calls."""
    settings = LoopbackSettings(reply_after_ms=delay_ms, barge_in_reaction_ms=reaction_ms)
    with agent_process(settings) as url:
        plain = await _calls(url, calls, barge_in=False, first_seed=1)
        barged = await _calls(url, calls, barge_in=True, first_seed=1_000)
        agent = health(url)
    measured = plain + barged
    return CaseResult(
        delay_ms=delay_ms,
        reaction_ms=reaction_ms,
        calls=len(measured),
        statuses=sorted({call.result.status for call in measured}),
        underruns=sum(call.stats.underruns for call in measured),
        latency=error_summary([call.latencies_ms for call in measured], delay_ms, seed=1),
        reaction=error_summary([call.reactions_ms for call in barged], reaction_ms, seed=2),
        agent=agent["stats"],
    )


def describe(name: str, summary: ErrorSummary) -> str:
    """One report line."""
    bias, p95 = summary.bias_ms, summary.p95_ms
    return (
        f"  {name:9} n={summary.count:4} bias {bias.value:+5.1f} ms "
        f"[{bias.ci_low:+.1f}, {bias.ci_high:+.1f}]  |error| p95 {p95.value:4.1f} ms "
        f"[{p95.ci_low:.1f}, {p95.ci_high:.1f}]  worst {summary.worst_ms:4.1f} ms"
        f"  missing {summary.missing}"
    )


async def run(calls: int, tolerance_ms: float) -> list[CaseResult]:
    """Calibrate every case, print the report, and return the results."""
    gc.collect()
    gc.freeze()
    results = []
    for delay_ms, reaction_ms in CASES:
        result = await calibrate_case(delay_ms, reaction_ms, calls)
        results.append(result)
        verdict = "ok" if result.within(tolerance_ms) else f"FAIL (> {tolerance_ms:.0f} ms)"
        print(  # noqa: T201 -- the script's report
            f"delay {delay_ms:.0f} ms, reaction {reaction_ms:.0f} ms, {result.calls} calls, "
            f"{result.underruns} underruns: {verdict}\n"
            f"{describe('latency', result.latency)}\n{describe('barge-in', result.reaction)}"
        )
    return results


def main(argv: Sequence[str] | None = None) -> int:
    """Run the calibration; exit 1 when SC4 fails."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls", type=int, default=10, help="calls per case and kind")
    parser.add_argument("--tolerance-ms", type=float, default=20.0)
    parser.add_argument("--json", type=Path, default=None, help="write the results here")
    args = parser.parse_args(argv)
    results = anyio.run(run, args.calls, args.tolerance_ms)
    if args.json is not None:
        rows = [asdict(result) for result in results]
        args.json.write_text(json.dumps(rows, indent=2, default=json_default) + "\n")
    return 0 if all(result.within(args.tolerance_ms) for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
