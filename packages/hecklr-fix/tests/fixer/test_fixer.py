"""The pipeline on a simulated agent whose behaviour follows its prompt, like the rules brain."""

import itertools
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from hecklr_core import BudgetEstimateError, load_model
from hecklr_fix.editable import load_editable
from hecklr_fix.errors import FixError
from hecklr_fix.fixer import Fixer, FixOutcome, FixSettings, PassRequest
from hecklr_fix.patching import ProposedEdit, ProposedPatch
from hecklr_fix.target import FixTarget
from hecklr_fix.validation import SuiteRun
from hecklr_fix.workspace import TARGETED_DIR, TARGETED_SUITE
from hecklr_llm import FakeLLM
from hecklr_schemas import ClusterId, FailureCluster, HecklrConfig, PatchStatus, RunStatus, new_id

from .factories import FAST_STATS, make_run, scenario, suite_run

pytestmark = pytest.mark.anyio

BOOKING = [scenario(f"book-{i}", ["booking"]) for i in range(3)]
REFUNDS = [scenario(f"refund-{i}", ["refunds"]) for i in range(3)]
OTHER = [scenario(f"other-{i}", ["core"]) for i in range(2)]
GUARD = BOOKING + REFUNDS + OTHER
TRIALS = 3


def _patch(find: str, replace: str, file: str = "prompts/system.md") -> ProposedPatch:
    return ProposedPatch(
        rationale=f"replace {find!r}", edits=[ProposedEdit(file=file, find=find, replace=replace)]
    )


GOOD = _patch("Book the first free slot.", "Before booking, confirm the exact slot.")
PLANTED_BAD = _patch(
    "- Book the first free slot.\n- Verify before refunds.",
    "- Before booking, confirm the slot.\n- Refund straight away.",
)
IRRELEVANT = _patch("Rules:", "Rules (be kind):")
FORBIDDEN = _patch("id: a", "id: b", file="scenarios/a.yaml")


def behaves(prompt: str, scenario_id: str) -> bool:
    """The simulated agent: booking needs the confirm rule, refunds need the verify rule."""
    if scenario_id.startswith("book"):
        return "confirm" in prompt
    if scenario_id.startswith("refund"):
        return "Verify before refunds" in prompt
    return True


@dataclass
class FakeRunner:
    """Runs a workspace by reading its prompt; records every request."""

    spent: str = "0"
    status: RunStatus = RunStatus.COMPLETED
    fail_on: Callable[[PassRequest], bool] = lambda request: False
    requests: list[PassRequest] = field(default_factory=list)

    async def __call__(self, request: PassRequest) -> SuiteRun:
        self.requests.append(request)
        if self.fail_on(request):
            raise BudgetEstimateError(Decimal("1"), Decimal("0.5"), 9)
        root = request.config_path.parent
        prompt = (root / "prompts" / "system.md").read_text()
        config = yaml.safe_load(request.config_path.read_text())
        if request.suite == TARGETED_SUITE:
            assert config["suites"][TARGETED_SUITE]["scenarios"] == f"{TARGETED_DIR}/*.yaml"
            ids = sorted(p.stem for p in (root / TARGETED_DIR).glob("*.yaml"))
            scenarios = [s for s in GUARD if s.id in ids]
        else:
            scenarios = GUARD
        trials = request.trials or TRIALS
        outcomes = {s.id: [behaves(prompt, s.id)] * trials for s in scenarios}
        return suite_run(make_run(outcomes, status=self.status, spent=self.spent), scenarios)


@dataclass
class FakeLauncher:
    """Records launches instead of starting processes."""

    launches: list[tuple[str, str, int]] = field(default_factory=list)

    def launch(
        self, workspace: Path, host: str, port: int, log: Path
    ) -> AbstractAsyncContextManager[None]:
        assert workspace.is_dir()
        self.launches.append((workspace.name, host, port))

        @asynccontextmanager
        async def running() -> AsyncIterator[None]:
            yield

        return running()


def _target() -> FixTarget:
    cluster = FailureCluster(
        id=ClusterId(new_id()),
        label="books without confirming",
        source="run",
        created_at=datetime.now(UTC),
    )
    return FixTarget(cluster=cluster, scenarios=tuple(BOOKING))


def _fixer(
    project: Path,
    script: Sequence[ProposedPatch],
    *,
    runner: FakeRunner | None = None,
    budget: str | None = None,
    candidates: int | None = None,
) -> tuple[Fixer, FakeRunner, FakeLauncher, FakeLLM]:
    config = load_model(project, HecklrConfig)
    llm = FakeLLM(script=list(script))
    runner = runner or FakeRunner()
    launcher = FakeLauncher()
    settings = FixSettings(
        model="fake/fixer",
        guard_suite="ci",
        seed=11,
        stats=FAST_STATS,
        candidates=candidates if candidates is not None else len(script),
        targeted_trials=TRIALS,
        budget_usd=Decimal(budget) if budget is not None else None,
    )
    fixer = Fixer(
        config_path=project,
        editable=load_editable(config, project),
        target=_target(),
        settings=settings,
        llm=llm,
        runner=runner,
        launcher=launcher,
        session_dir=project.parent / ".hecklr" / "fix" / "s1",
        runs_output_dir=project.parent / ".hecklr" / "fix",
        ports=itertools.count(40_000).__next__,
    )
    return fixer, runner, launcher, llm


async def test_ws15_good_patch_wins_and_the_planted_bad_patch_is_rejected_by_the_guard(
    project: Path,
) -> None:
    fixer, runner, launcher, _ = _fixer(project, [GOOD, PLANTED_BAD, IRRELEVANT, FORBIDDEN])

    result = await fixer.run()

    assert result.outcome is FixOutcome.FIXED
    statuses = {p.candidate_index: p.status for p in result.patches}
    assert statuses == {
        0: PatchStatus.ACCEPTED,
        1: PatchStatus.REJECTED_GUARD,
        2: PatchStatus.REJECTED_TARGETED,
    }
    assert [r.index for r in result.rejected] == [3]
    assert result.winner is not None
    assert result.winner.candidate_index == 0
    winner = result.winner
    assert winner.files == ["prompts/system.md"]
    assert result.winner_files == {
        "prompts/system.md": "Rules:\n- Before booking, confirm the exact slot.\n"
        "- Verify before refunds.\n"
    }
    assert winner.targeted[0].verdict.value == "improvement"
    assert winner.guard
    assert all(c.verdict.value != "regression" for c in winner.guard)
    bad = result.patches[1]
    assert {"tag": "refunds"} in [c.slice for c in bad.guard if c.verdict.value == "regression"]
    assert [name for name, _, _ in launcher.launches] == [
        "baseline-targeted",
        "candidate-0-targeted",
        "candidate-1-targeted",
        "candidate-2-targeted",
        "baseline-guard",
        "candidate-0-guard",
        "candidate-1-guard",
    ]
    assert len({port for _, _, port in launcher.launches}) == len(launcher.launches)
    assert {r.seed for r in runner.requests} == {11}
    assert {r.output_dir for r in runner.requests} == {fixer.runs_output_dir}
    assert [r.trials for r in runner.requests] == [3, 3, 3, 3, None, None, None]
    assert result.targeted_baseline == result.patches[0].targeted[0].baseline
    assert result.guard_baseline == result.patches[0].guard[0].baseline
    assert not (fixer.session_dir / "work").exists() or not any(
        (fixer.session_dir / "work").iterdir()
    )
    # The user's prompt was never touched.
    assert "first free slot" in (project.parent / "prompts" / "system.md").read_text()


async def test_us10_no_candidate_beat_baseline_is_reported_honestly(project: Path) -> None:
    fixer, runner, _, _ = _fixer(project, [IRRELEVANT, FORBIDDEN])

    result = await fixer.run()

    assert result.outcome is FixOutcome.NO_CANDIDATE_BEAT_BASELINE
    assert result.message == "no candidate beat the baseline"
    assert result.winner is None
    assert result.guard_baseline is None
    assert len(runner.requests) == 2  # targeted baseline + the one valid candidate


async def test_a_baseline_that_already_passes_needs_no_patch(project: Path) -> None:
    prompt = project.parent / "prompts" / "system.md"
    prompt.write_text(prompt.read_text().replace("Book the first free slot.", "Always confirm."))
    fixer, _, _, llm = _fixer(project, [GOOD])

    result = await fixer.run()

    assert result.outcome is FixOutcome.BASELINE_PASSES
    assert llm.calls == []


async def test_the_budget_stops_the_session_with_what_is_known(project: Path) -> None:
    fixer, runner, _, _ = _fixer(
        project, [GOOD, IRRELEVANT], runner=FakeRunner(spent="0.03"), budget="0.05"
    )

    result = await fixer.run()

    assert result.outcome is FixOutcome.BUDGET_EXHAUSTED
    assert "was spent before candidate-1-targeted" in result.message
    assert [p.status for p in result.patches] == [PatchStatus.PROPOSED, PatchStatus.PROPOSED]
    assert result.patches[0].targeted  # candidate 0 was measured before the money ran out
    assert result.spent_usd == Decimal("0.06")
    assert [r.budget_usd for r in runner.requests] == [Decimal("0.05"), Decimal("0.02")]


async def test_a_winner_found_before_the_budget_ran_out_is_still_the_winner(
    project: Path,
) -> None:
    runner = FakeRunner(fail_on=lambda r: "candidate-1" in str(r.config_path))
    fixer, _, _, _ = _fixer(project, [GOOD, IRRELEVANT], runner=runner)
    # Candidate 1's targeted run is refused, so the guard pass never starts: nothing accepted.
    result = await fixer.run()
    assert result.outcome is FixOutcome.BUDGET_EXHAUSTED

    runner = FakeRunner(fail_on=lambda r: "candidate-1-guard" in str(r.config_path))
    fixer, _, _, _ = _fixer(project, [GOOD, PLANTED_BAD], runner=runner)
    result = await fixer.run()

    assert result.outcome is FixOutcome.FIXED
    assert "not every candidate was evaluated" in result.message
    assert result.winner is not None
    assert result.winner.candidate_index == 0


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (RunStatus.BUDGET_EXCEEDED, "budget ran out during baseline-targeted"),
        (RunStatus.FAILED, None),
    ],
)
async def test_runs_that_did_not_complete(
    project: Path, status: RunStatus, expected: str | None
) -> None:
    fixer, _, _, _ = _fixer(project, [GOOD], runner=FakeRunner(status=status))

    if expected is None:
        with pytest.raises(FixError, match=r"\(baseline-targeted\) ended failed"):
            await fixer.run()
    else:
        result = await fixer.run()
        assert result.outcome is FixOutcome.BUDGET_EXHAUSTED
        assert expected in result.message


async def test_a_baseline_without_any_analyzable_trial_is_an_error(project: Path) -> None:
    class HarnessErrors(FakeRunner):
        async def __call__(self, request: PassRequest) -> SuiteRun:
            return suite_run(make_run({"book-0": [None]}), BOOKING)

    fixer, _, _, _ = _fixer(project, [GOOD], runner=HarnessErrors())

    with pytest.raises(FixError, match="could be analyzed"):
        await fixer.run()


async def test_the_largest_targeted_improvement_wins(project: Path) -> None:
    partial = _patch("Rules:", "Rules (confirm):")  # "confirm" anywhere fixes the fake agent too
    fixer, _, _, _ = _fixer(project, [partial, GOOD])

    result = await fixer.run()

    # Both fix every booking trial: equal deltas, so the earlier candidate wins the tie.
    assert result.winner is not None
    assert result.winner.candidate_index == 0
    assert [p.status for p in result.patches] == [PatchStatus.ACCEPTED, PatchStatus.ACCEPTED]


def test_settings_must_ask_for_work(project: Path) -> None:
    with pytest.raises(FixError, match="at least one candidate"):
        _fixer(project, [GOOD], candidates=0)
