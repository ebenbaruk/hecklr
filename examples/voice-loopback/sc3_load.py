"""SC3: many simultaneous calls from one harness to the networked loopback agent.

For each call count, start a fresh loopback agent in its own process, place that many calls at
once from this process (one event loop, as ``hecklr run`` does; half the callers barge in), and
report frame underruns, send lateness, event-loop lag, latency and barge-in errors against the
injected truth, and CPU use of the harness loop, its DSP workers and the agent::

    uv run python examples/voice-loopback/sc3_load.py --calls 50 100 150 --json sc3.json

SC3 asks for 50 calls on an 8-core machine without frame drops (no underruns).
"""

import argparse
import gc
import json
import os
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

import anyio
from loopback_agent.bench import (
    CallMeasurement,
    ErrorSummary,
    LoopLag,
    agent_process,
    child_pids,
    cpu_seconds,
    error_summary,
    health,
    json_default,
    place_calls,
)
from loopback_agent.call import LoopbackSettings

DELAY_MS: Final = 600.0
REACTION_MS: Final = 200.0


@dataclass(frozen=True, slots=True)
class LoadResult:
    """One load level."""

    calls: int
    wall_s: float
    statuses: dict[str, int]
    underruns: int
    calls_with_underruns: int
    worst_send_lateness_ms: float
    worst_loop_lag_ms: float
    latency: ErrorSummary
    reaction: ErrorSummary
    harness_loop_cores: float
    """CPU of the harness process (its event loop and threads) per wall second."""
    dsp_worker_cores: float
    agent_cores: float
    load_average: tuple[float, float, float]
    """The machine's 1, 5 and 15 minute load averages at the end: other work skews timing."""

    @property
    def sc3_ok(self) -> bool:
        """Every call completed and no caller frame was sent a frame period late."""
        return self.underruns == 0 and set(self.statuses) == {"ok"}


async def load_level(calls: int) -> LoadResult:
    """Place ``calls`` simultaneous calls to a fresh agent and measure the harness."""
    settings = LoopbackSettings(reply_after_ms=DELAY_MS, barge_in_reaction_ms=REACTION_MS)
    with agent_process(settings) as url:
        agent_before = _agent_cpu(url)
        results: list[CallMeasurement] = []
        lag = LoopLag()
        workers = child_pids()
        own_before, workers_before = _own_cpu(), cpu_seconds(workers)
        started = time.monotonic()

        async def batch(count: int, barge_in: bool, first_seed: int) -> None:
            results.extend(await place_calls(url, count, barge_in=barge_in, first_seed=first_seed))

        async with anyio.create_task_group() as group:
            group.start_soon(lag.run)
            async with anyio.create_task_group() as callers:
                callers.start_soon(batch, calls // 2, False, 1)
                callers.start_soon(batch, calls - calls // 2, True, 10_000)
            group.cancel_scope.cancel()
        wall = time.monotonic() - started
        workers = sorted(set(workers) | set(child_pids()))
        own, dsp = _own_cpu() - own_before, cpu_seconds(workers) - workers_before
        agent = _agent_cpu(url) - agent_before
    statuses: dict[str, int] = {}
    for result in results:
        statuses[result.result.status] = statuses.get(result.result.status, 0) + 1
    return LoadResult(
        calls=calls,
        wall_s=wall,
        statuses=statuses,
        underruns=sum(result.stats.underruns for result in results),
        calls_with_underruns=sum(result.stats.underruns > 0 for result in results),
        worst_send_lateness_ms=max(result.stats.max_lateness_ms for result in results),
        worst_loop_lag_ms=lag.worst_ms,
        latency=error_summary([result.latencies_ms for result in results], DELAY_MS, seed=1),
        reaction=error_summary(
            [result.reactions_ms for result in results if result.reactions_ms], REACTION_MS, seed=2
        ),
        harness_loop_cores=own / wall,
        dsp_worker_cores=dsp / wall,
        agent_cores=agent / wall,
        load_average=os.getloadavg(),
    )


def _own_cpu() -> float:
    times = os.times()
    return times.user + times.system


def _agent_cpu(url: str) -> float:
    cpu = health(url)["cpu_s"]
    return float(cpu) if isinstance(cpu, int | float) else 0.0


def describe(result: LoadResult) -> str:
    """The report lines of one level."""
    latency, reaction = result.latency, result.reaction
    return (
        f"{result.calls} calls ({result.wall_s:.0f} s): statuses {result.statuses}, "
        f"underruns {result.underruns} in {result.calls_with_underruns} call(s), "
        f"worst send lateness {result.worst_send_lateness_ms:.1f} ms, "
        f"worst loop lag {result.worst_loop_lag_ms:.1f} ms -> SC3 "
        f"{'ok' if result.sc3_ok else 'FAIL'}\n"
        f"  latency error: p95 {latency.p95_ms.value:.1f} ms "
        f"[{latency.p95_ms.ci_low:.1f}, {latency.p95_ms.ci_high:.1f}], "
        f"worst {latency.worst_ms:.1f} ms over {latency.count} turns\n"
        f"  barge-in error: p95 {reaction.p95_ms.value:.1f} ms "
        f"[{reaction.p95_ms.ci_low:.1f}, {reaction.p95_ms.ci_high:.1f}], "
        f"worst {reaction.worst_ms:.1f} ms over {reaction.count}, missing {reaction.missing}\n"
        f"  CPU cores: harness loop {result.harness_loop_cores:.2f}, "
        f"DSP workers {result.dsp_worker_cores:.2f}, agent {result.agent_cores:.2f} "
        f"(machine has {os.cpu_count()}; load average {result.load_average[0]:.1f})"
    )


async def run(levels: Sequence[int]) -> list[LoadResult]:
    """Measure every level, print the report, and return the results."""
    gc.collect()
    gc.freeze()
    results = []
    for calls in levels:
        result = await load_level(calls)
        results.append(result)
        print(describe(result), flush=True)  # noqa: T201 -- the script's report
    return results


def main(argv: Sequence[str] | None = None) -> int:
    """Run the load levels."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls", type=int, nargs="+", default=[50, 100])
    parser.add_argument("--json", type=Path, default=None, help="write the results here")
    args = parser.parse_args(argv)
    results = anyio.run(run, args.calls)
    if args.json is not None:
        rows = [{**asdict(result), "sc3_ok": result.sc3_ok} for result in results]
        args.json.write_text(json.dumps(rows, indent=2, default=json_default) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
