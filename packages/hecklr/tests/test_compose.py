"""Composition: real world sandbox, chat driver, python adapter, simulator and graders."""

import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from hecklr.compose import (
    ChannelUnavailableError,
    RunRequest,
    UnknownBaselineError,
    detect_git_sha,
    execute_run,
    prepare_run,
)
from hecklr.testing import scripted_models
from hecklr_adapters import AdapterConfigError
from hecklr_core import BudgetEstimateError, ConfigError
from hecklr_grade import InvalidCriterionError
from hecklr_llm import FakeLLM
from hecklr_report.outcomes import convert
from hecklr_schemas import Channel, Grade, LLMRequest, RunStatus, Trial

Edit = Callable[[Path, str, str], None]

# The world sandbox and the agents under test listen on 127.0.0.1 ephemeral ports.
pytestmark = [pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]


def _request(config_path: Path, **overrides: object) -> RunRequest:
    return RunRequest(config_path=config_path, suite="ci", **overrides)  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_us01_run_stores_graded_trials_for_every_scenario(config_path: Path) -> None:
    stored: list[Trial] = []

    def on_trial(trial: Trial, grades: object) -> None:
        stored.append(trial)

    prepared = prepare_run(_request(config_path, trials=2))
    results = await execute_run(prepared, llm_providers=scripted_models(), on_trial=on_trial)

    assert results.run.status is RunStatus.COMPLETED
    assert len(results.trials) == len(stored) == 10
    assert {trial.status for trial in results.trials} == {"ok"}
    assert {trial.scenario_id for trial in results.trials} == {
        "check-appointment", "plan-question", "reschedule-visit", "saturday-visits",
        "unknown-customer",
    }  # fmt: skip
    # The starter agent's tool calls reached the world sandbox and changed it.
    reschedule = next(t for t in results.trials if t.scenario_id == "reschedule-visit")
    assert (
        reschedule.world_after.collections["appointments"]["C-1002"]["slot"] == "2026-11-13T09:00"
    )
    criterion_grades = [g for g in results.grades if g.criterion_index is not None]
    assert criterion_grades
    assert all(g.status == "ok" and g.passed for g in criterion_grades)
    suite = prepared.plan.suite
    outcomes = convert(results, suite.scenarios, suite.personas).outcomes
    assert outcomes
    assert all(outcome.passed for outcome in outcomes)
    assert (prepared.runs_dir / results.run.id / "results.duckdb").is_file()


@pytest.mark.anyio
async def test_grading_runs_through_hecklr_grade_with_judges_and_tools(config_path: Path) -> None:
    def judge_passes(request: LLMRequest) -> bool:
        return "saturday" not in request.messages[-1].content.lower()

    prepared = prepare_run(_request(config_path, trials=1))
    results = await execute_run(prepared, llm_providers=scripted_models(judge_passes))

    by_scenario = {
        trial.scenario_id: [g for g in results.grades if g.trial_id == trial.id]
        for trial in results.trials
    }
    saturday: list[Grade] = by_scenario["saturday-visits"]
    assert [(g.grader, g.passed) for g in saturday] == [
        ("not_contains", True),
        ("judge:tone", False),
    ]
    assert [g.grader for g in by_scenario["reschedule-visit"]] == [
        "tool_called", "state", "judge:resolved",
    ]  # fmt: skip
    assert all(g.judge_model == "fake/judge" for g in saturday if g.grader.startswith("judge"))


@pytest.mark.anyio
async def test_adaptive_pass_adds_trials_to_the_same_run(config_path: Path) -> None:
    extra: list[int] = []
    prepared = prepare_run(_request(config_path, trials=2, adaptive=True))

    results = await execute_run(prepared, llm_providers=scripted_models(), on_adaptive=extra.append)

    assert results.run.adaptive
    assert extra == [5]  # half of the 10 first-pass trials, rounded up
    assert len(results.trials) == len(results.run.matrix) == 15


@pytest.mark.anyio
async def test_adaptive_pass_weighs_scenarios_that_flipped_vs_the_baseline(
    config_path: Path,
) -> None:
    baseline = await execute_run(
        prepare_run(_request(config_path, trials=2)), llm_providers=scripted_models()
    )

    def judge_passes(request: LLMRequest) -> bool:
        return "plan you are subscribed to" not in request.messages[-1].content

    prepared = prepare_run(
        _request(config_path, trials=2, adaptive=True, baseline_id=baseline.run.id)
    )
    results = await execute_run(prepared, llm_providers=scripted_models(judge_passes))

    assert results.run.baseline_id == baseline.run.id
    extra = [spec for spec in results.run.matrix if spec.index >= 2]
    flipped = [spec for spec in extra if spec.scenario_id == "plan-question"]
    assert len(flipped) == max(
        sum(spec.scenario_id == sid for spec in extra) for sid in {s.scenario_id for s in extra}
    )


@pytest.mark.anyio
async def test_adaptive_pass_is_skipped_when_nothing_could_be_counted(config_path: Path) -> None:
    prepared = prepare_run(_request(config_path, trials=1, adaptive=True))

    # A stock FakeLLM cannot play a customer, so every trial is a harness error.
    results = await execute_run(prepared, llm_providers={"fake": FakeLLM()})

    assert not results.run.adaptive
    assert {trial.status for trial in results.trials} == {"harness_error"}


def test_seed_and_output_dir_overrides(config_path: Path, tmp_path: Path) -> None:
    default = prepare_run(_request(config_path))
    reseeded = prepare_run(_request(config_path, seed=99, output_dir=tmp_path / "out"))

    assert reseeded.plan.run.seed == 99
    assert [s.seed for s in reseeded.plan.run.matrix] != [s.seed for s in default.plan.run.matrix]
    assert reseeded.runs_dir == (tmp_path / "out" / "runs").resolve()
    assert default.runs_dir == config_path.parent / ".hecklr" / "runs"


def test_trials_channels_and_budget_flags_shape_the_plan(config_path: Path) -> None:
    prepared = prepare_run(
        _request(config_path, trials=1, channels=[Channel.CHAT], budget_usd=Decimal("0.5"))
    )

    assert len(prepared.plan.run.matrix) == 5
    assert prepared.plan.run.budget.usd == Decimal("0.5")
    assert set(prepared.drivers) == {Channel.CHAT}
    assert not prepared.adaptive


def test_suite_adaptive_setting_is_the_default(config_path: Path, edit: Edit) -> None:
    edit(config_path, "trials: 3,", "trials: 3, adaptive: true,")

    assert prepare_run(_request(config_path)).adaptive
    assert not prepare_run(_request(config_path, adaptive=False)).adaptive


def test_voice_suite_names_the_missing_driver(config_path: Path, edit: Edit) -> None:
    project = config_path.parent
    edit(
        config_path,
        "  adapters:\n",
        "  adapters:\n    voice: { type: pipecat, url: http://localhost:7860/offer }\n",
    )
    edit(config_path, "channels: [chat] }", "channels: [chat, voice] }")
    voice = "voice: { tts: { provider: kokoro, voice: af_bella } }\n"
    for persona in (project / "personas").glob("*.yaml"):
        persona.write_text(persona.read_text() + voice)
    for scenario in (project / "scenarios").glob("*.yaml"):
        edit(scenario, "channels: [chat]", "channels: [chat, voice]")

    with pytest.raises(ChannelUnavailableError, match=r"hecklr\[voice\].*--channels chat"):
        prepare_run(_request(config_path))
    assert prepare_run(_request(config_path, channels=[Channel.CHAT])).drivers


def test_unknown_baseline_is_refused(config_path: Path) -> None:
    with pytest.raises(UnknownBaselineError, match="no stored run"):
        prepare_run(_request(config_path, baseline_id="01ARZ3NDEKTSV4RRFFQ69G5FAV"))
    with pytest.raises(UnknownBaselineError):
        prepare_run(_request(config_path, baseline_id="not-a-ulid"))


def test_unparsable_criterion_fails_before_the_run(config_path: Path, edit: Edit) -> None:
    scenario = config_path.parent / "scenarios" / "saturday-visits.yaml"
    edit(
        scenario,
        "  - type: not_contains\n    text: traceback\n",
        "  - type: regex\n    pattern: '(unclosed'\n",
    )

    with pytest.raises(InvalidCriterionError, match="invalid regex"):
        prepare_run(_request(config_path))


def test_unimportable_python_agent_fails_before_the_run(config_path: Path, edit: Edit) -> None:
    edit(config_path, "hecklr_agent:reply", "missing_module:reply")

    with pytest.raises(AdapterConfigError, match="missing_module"):
        prepare_run(_request(config_path))


def test_python_agent_is_imported_from_the_project_directory(config_path: Path) -> None:
    prepare_run(_request(config_path))

    assert sys.path[0] == str(config_path.parent)
    prepare_run(_request(config_path))
    assert sys.path.count(str(config_path.parent)) == 1


def test_budget_refusal_and_config_errors_surface_from_prepare(
    config_path: Path, edit: Edit
) -> None:
    edit(config_path, "simulator: fake/customer", "simulator: anthropic/claude-sonnet-5-5")
    with pytest.raises(BudgetEstimateError):
        prepare_run(_request(config_path, budget_usd=Decimal("0.01")))
    with pytest.raises(ConfigError, match="unknown suite"):
        prepare_run(RunRequest(config_path=config_path, suite="nightly"))


def test_git_sha_comes_from_ci_then_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    assert detect_git_sha(tmp_path) == "abc123"
    monkeypatch.delenv("GITHUB_SHA")
    assert detect_git_sha(tmp_path) is None
    assert detect_git_sha(Path(__file__).parent) is not None


@pytest.mark.anyio
async def test_fake_models_default_to_the_scripted_customer_and_judge(config_path: Path) -> None:
    prepared = prepare_run(_request(config_path, trials=1))

    results = await execute_run(prepared)

    assert {trial.status for trial in results.trials} == {"ok"}
