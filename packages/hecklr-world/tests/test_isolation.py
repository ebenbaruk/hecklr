"""Concurrent trials are isolated and deterministic (property-based)."""

from collections.abc import Callable
from typing import Any

import anyio
import anyio.lowlevel
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hecklr_schemas import TrialBrief, World
from hecklr_world import TrialWorld

BriefFactory = Callable[..., TrialBrief]
SinkFactory = Callable[..., Any]

Call = tuple[str, dict[str, Any]]

_slots = st.sampled_from(["2026-10-27T09:00", "2026-10-28T10:00", "2026-11-03T08:00"])
_appointments = st.sampled_from(["A-1", "A-2", "A-3"])
_calls: st.SearchStrategy[Call] = st.one_of(
    st.builds(
        lambda a, s: ("reschedule_appointment", {"appointment_id": a, "slot": s}),
        _appointments,
        _slots,
    ),
    st.builds(
        lambda a: ("cancel_appointment", {"appointment_id": a, "status": "cancelled"}),
        _appointments,
    ),
    st.builds(
        lambda amount: ("issue_refund", {"invoice_id": "INV-501", "amount_eur": amount}),
        st.integers(min_value=1, max_value=500),
    ),
    st.builds(
        lambda c: ("escalate_emergency", {"customer_id": c, "reason": "gas"}),
        st.sampled_from(["C-1042", "C-2077"]),
    ),
    st.builds(lambda c: ("get_customer", {"customer_id": c}), st.sampled_from(["C-1042", "C-9"])),
)
_scripts = st.lists(_calls, max_size=12)


async def _run_alone(world: World, brief: TrialBrief, sink: Any, script: list[Call]) -> Any:
    trial = TrialWorld(brief, world, sink)
    for name, args in script:
        await trial.call_tool(name, args)
    return trial.snapshot().collections


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(left=_scripts, right=_scripts)
def test_two_concurrent_trials_never_see_each_others_writes(
    example_world: World,
    make_brief: BriefFactory,
    make_sink: SinkFactory,
    left: list[Call],
    right: list[Call],
) -> None:
    async def scenario() -> None:
        briefs = [make_brief(seed=1), make_brief(seed=2)]
        trials = [TrialWorld(b, example_world, make_sink(b.trial_id)) for b in briefs]

        async def play(trial: TrialWorld, script: list[Call]) -> None:
            for name, args in script:
                await trial.call_tool(name, args)
                await anyio.lowlevel.checkpoint()  # let the two trials interleave

        async with anyio.create_task_group() as tg:
            tg.start_soon(play, trials[0], left)
            tg.start_soon(play, trials[1], right)

        for trial, brief, script in zip(trials, briefs, (left, right), strict=True):
            alone = await _run_alone(example_world, brief, make_sink(brief.trial_id), script)
            assert trial.snapshot().collections == alone

    anyio.run(scenario)


@settings(
    max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(script=_scripts, seed=st.integers(min_value=0, max_value=2**32))
def test_a_trial_replays_identically_from_its_seed(
    example_world: World,
    make_brief: BriefFactory,
    make_sink: SinkFactory,
    script: list[Call],
    seed: int,
) -> None:
    faults = [
        {"tool": "get_customer", "kind": "http_500", "probability": 0.5},
        {"tool": "issue_refund", "kind": "stale_data", "probability": 0.5, "patch": {"x": 1}},
    ]

    async def replay() -> list[Any]:
        brief = make_brief(faults=faults, seed=seed)
        sink = make_sink(brief.trial_id)
        trial = TrialWorld(brief, example_world, sink)
        bodies = [(await trial.call_tool(n, a)).body() for n, a in script]
        return [bodies, trial.snapshot().collections, [r.fault for r in sink.results()]]

    assert anyio.run(replay) == anyio.run(replay)
