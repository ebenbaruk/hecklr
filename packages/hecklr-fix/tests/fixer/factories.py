"""Synthetic runs from the real schema models, and a tiny agent project on disk."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from hecklr_core import RunResults
from hecklr_fix.validation import SuiteRun
from hecklr_schemas import (
    AgentRef,
    Budget,
    Channel,
    Event,
    EventId,
    Grade,
    GradeId,
    Run,
    RunId,
    RunStatus,
    Scenario,
    StatsConfig,
    Trial,
    TrialId,
    UtterancePayload,
    WorldSnapshot,
    new_id,
)

FAST_STATS = StatsConfig(bootstrap_resamples=1_000, seed=7)
WORLD = WorldSnapshot(world_id="acme", taken_at_ms=0, collections={})


def scenario(scenario_id: str, tags: Sequence[str] = ("core",)) -> Scenario:
    """A scenario with one judge criterion."""
    return Scenario.model_validate(
        {
            "id": scenario_id,
            "title": scenario_id,
            "channels": ["chat"],
            "tags": list(tags),
            "persona": "busy-homeowner",
            "world": "acme",
            "goal": f"Goal of {scenario_id}",
            "facts": {"customer_id": "C-1"},
            "success": [{"type": "judge", "rubric": "resolved"}],
        }
    )


def make_run(
    outcomes: Mapping[str, Sequence[bool | None]],
    *,
    status: RunStatus = RunStatus.COMPLETED,
    spent: str = "0",
) -> RunResults:
    """A run where scenario ``s`` has one trial per entry of ``outcomes[s]``.

    ``True``/``False`` pass/fail on the judge; ``None`` is a harness error.
    """
    run = Run(
        id=RunId(new_id()),
        created_at=datetime(2026, 10, 23, tzinfo=UTC),
        agent=AgentRef(name="acme"),
        config_hash="0" * 64,
        seed=1,
        suite="s",
        matrix=[],
        budget=Budget(),
        status=status,
        spent_usd=Decimal(spent),
    )
    trials: list[Trial] = []
    events: list[Event] = []
    grades: list[Grade] = []
    for scenario_id, results in outcomes.items():
        for index, passed in enumerate(results):
            trial_id = TrialId(new_id())
            trials.append(
                Trial(
                    id=trial_id,
                    run_id=run.id,
                    scenario_id=scenario_id,
                    persona_id="busy-homeowner",
                    channel=Channel.CHAT,
                    index=index,
                    seed=index,
                    status="harness_error" if passed is None else "ok",
                    started_at=run.created_at,
                    duration_ms=10,
                    cost_usd=Decimal(0),
                    world_before=WORLD,
                    world_after=WORLD,
                )
            )
            for seq, (actor, text) in enumerate(
                [("user", "Book me Tuesday please"), ("agent", "Booked Monday 9:00.")]
            ):
                events.append(
                    Event(
                        id=EventId(new_id()),
                        trial_id=trial_id,
                        seq=seq,
                        t_ms=seq * 100,
                        actor=actor,  # type: ignore[arg-type]  # the literal pair above
                        payload=UtterancePayload(text=text, turn=seq),
                    )
                )
            if passed is not None:
                grades.append(
                    Grade(
                        id=GradeId(new_id()),
                        trial_id=trial_id,
                        grader="judge:resolved",
                        grader_version="1",
                        criterion_index=0,
                        value=passed,
                        passed=passed,
                        rationale=None if passed else "booked a slot the customer never chose",
                    )
                )
    return RunResults(run=run, trials=trials, events=events, grades=grades)


def suite_run(results: RunResults, scenarios: Sequence[Scenario]) -> SuiteRun:
    """``results`` with its definitions."""
    return SuiteRun(results=results, scenarios={s.id: s for s in scenarios}, personas={})


PROJECT_FILES: Mapping[str, str] = {
    "hecklr.yaml": (
        "version: 1\n"
        "agent:\n"
        "  name: acme\n"
        "  adapters:\n"
        "    chat: {type: http, url: 'http://127.0.0.1:8000/chat/stream', stream: sse}\n"
        "  editable: [prompts/system.md]\n"
        "world: worlds/acme.yaml\n"
        "suites:\n"
        "  ci: {scenarios: 'scenarios/**/*.yaml', trials: 2}\n"
        "models: {simulator: fake/sim, judge: fake/judge}\n"
    ),
    "worlds/acme.yaml": "id: acme\n",
    "scenarios/a.yaml": "id: a\n",
    "prompts/system.md": "Rules:\n- Book the first free slot.\n- Verify before refunds.\n",
    "agent/serve.py": "print('agent')\n",
    ".hecklr/runs/old/results.duckdb": "old run\n",
    ".git/HEAD": "ref: refs/heads/main\n",
    "agent/__pycache__/x.pyc": "cache",
}


def write_project(root: Path) -> Path:
    """A tiny agent project under ``root``; returns its ``hecklr.yaml``."""
    for relative, text in PROJECT_FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root / "hecklr.yaml"
