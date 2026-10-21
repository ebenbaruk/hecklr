"""Fixtures: starter projects written by ``hecklr init``'s scaffold, run on scripted models, and
synthetic runs written to a real ``ResultStore`` for ``compare``, ``gate``, ``report`` tests.

A synthetic trial gets one deterministic criterion grade, so a run's pass rates are exactly what
the test asks for, at any number of scenarios (a starter project has only 5, too few for a
significant verdict).
"""

import importlib.util
import random
import sys
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import anyio
import pytest
import structlog

from hecklr.scaffold import STARTER_AGENT_MODULE, AgentChoice, ModelChoice, write_project
from hecklr_core import ResultStore
from hecklr_schemas import (
    AgentRef,
    Budget,
    Channel,
    Grade,
    GradeId,
    Run,
    RunId,
    RunStatus,
    Trial,
    TrialId,
    WorldSnapshot,
    new_id,
)

FAKE_MODELS = ModelChoice("fake/customer", "fake/judge", "tests")


@pytest.fixture(autouse=True)
def restored_logging() -> Iterator[None]:
    """The CLI configures structlog for its process; give it back to the other tests."""
    saved = structlog.get_config()
    yield
    structlog.configure(**saved)


@pytest.fixture(autouse=True)
def isolated_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test imports its own project's starter agent and leaves ``sys.path`` as it was."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, STARTER_AGENT_MODULE, raising=False)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A starter project with the in-process agent and ``fake/`` models."""
    root = tmp_path / "shop"
    write_project(root, "shop-agent", AgentChoice("python"), FAKE_MODELS)
    return root


@pytest.fixture
def config_path(project_dir: Path) -> Path:
    """The starter project's ``hecklr.yaml``."""
    return project_dir / "hecklr.yaml"


@pytest.fixture
def edit() -> Callable[[Path, str, str], None]:
    """Replace a snippet (which must be present) in a project file."""

    def replace(path: Path, old: str, new: str) -> None:
        text = path.read_text(encoding="utf-8")
        assert old in text, f"{old!r} not in {path}"
        path.write_text(text.replace(old, new), encoding="utf-8")

    return replace


@pytest.fixture
def http_agent_url() -> Iterator[str]:
    """The chat URL of ``fixtures/http_agent.py``, served on an ephemeral 127.0.0.1 port."""
    path = Path(__file__).parent / "fixtures" / "http_agent.py"
    spec = importlib.util.spec_from_file_location("hecklr_fixture_http_agent", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with module.serve_in_thread() as url:
        yield url


# --- synthetic stored runs ------------------------------------------------------------------

_START = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
_EMPTY_WORLD = WorldSnapshot(world_id="acme-starter", taken_at_ms=0, collections={})


def store_run(
    runs_dir: Path,
    pass_rates: Mapping[str, float],
    *,
    trials: int = 3,
    suite: str = "ci",
    order: int = 0,
    seed: int = 0,
    status: RunStatus = RunStatus.COMPLETED,
    baseline_id: str | None = None,
) -> Run:
    """Store a run whose scenario ``s`` passes each of ``trials`` trials with ``pass_rates[s]``.

    ``order`` sets ``created_at`` (later runs are newer); ``seed`` draws the trial outcomes.
    """
    rng = random.Random(seed)
    run = Run(
        id=RunId(new_id()),
        created_at=_START + timedelta(minutes=order),
        agent=AgentRef(name="shop-agent"),
        config_hash="0" * 64,
        seed=seed,
        suite=suite,
        baseline_id=RunId(baseline_id) if baseline_id is not None else None,
        matrix=[],
        budget=Budget(usd=Decimal(5)),
        status=status,
    )

    async def write() -> None:
        async with ResultStore(runs_dir) as store:
            await store.write_run(run)
            for scenario_id, rate in pass_rates.items():
                for index in range(trials):
                    trial = _trial(run, scenario_id, index)
                    grade = _grade(trial.id, passed=rng.random() < rate)
                    await store.append_trial_result(trial, (), (grade,))

    anyio.run(write)
    return run


def _trial(run: Run, scenario_id: str, index: int) -> Trial:
    return Trial(
        id=TrialId(new_id()),
        run_id=run.id,
        scenario_id=scenario_id,
        persona_id="polite-regular",
        channel=Channel.CHAT,
        index=index,
        seed=index,
        status="ok",
        started_at=run.created_at,
        duration_ms=1_000,
        cost_usd=Decimal("0.001"),
        world_before=_EMPTY_WORLD,
        world_after=_EMPTY_WORLD,
        turns=2,
    )


def _grade(trial_id: TrialId, *, passed: bool) -> Grade:
    return Grade.model_validate(
        {
            "id": GradeId(new_id()),
            "trial_id": trial_id,
            "grader": "tool_called",
            "grader_version": "1",
            "criterion_index": 0,
            "status": "ok",
            "value": passed,
            "passed": passed,
            "rationale": None if passed else "the booking tool was never called",
        }
    )


StoreRun = Callable[..., Run]
"""``store(pass_rates, **options)``: ``store_run`` into the starter project's run store."""


@pytest.fixture
def runs_dir(project_dir: Path) -> Path:
    """Where the starter project stores its runs."""
    return project_dir / ".hecklr" / "runs"


@pytest.fixture
def store(runs_dir: Path) -> StoreRun:
    """Write synthetic runs into the starter project's run store."""

    def write(pass_rates: Mapping[str, float], **options: object) -> Run:
        return store_run(runs_dir, pass_rates, **options)  # type: ignore[arg-type]  # forwarded

    return write
