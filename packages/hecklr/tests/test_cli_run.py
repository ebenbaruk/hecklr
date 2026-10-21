"""``hecklr run`` through Typer's CliRunner, on starter projects and scripted models."""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import anyio
import click
import pytest
from typer.testing import CliRunner

import hecklr.commands.run
from hecklr import exit_codes
from hecklr.cli import app
from hecklr.commands._common import CliOverrides
from hecklr.commands.run import _exit_code
from hecklr.testing import scripted_models
from hecklr_core import ResultStore, RunResults
from hecklr_llm import ProviderConfigError
from hecklr_schemas import AgentRef, Budget, LLMRequest, Run, RunId, RunStatus, new_id

Edit = Callable[[Path, str, str], None]

# The world sandbox and the agents under test listen on 127.0.0.1 ephemeral ports.
pytestmark = [pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]

runner = CliRunner()
FAKE_MODEL_FLAGS = ["--simulator-model", "fake/customer", "--judge-model", "fake/judge"]


def _run(config_path: Path, *args: str, overrides: CliOverrides | None = None) -> tuple[int, str]:
    result = runner.invoke(
        app,
        ["run", "--config", str(config_path), *args],
        obj=overrides or CliOverrides(scripted_models()),
    )
    return result.exit_code, click.unstyle(result.output)


def _run_id(output: str) -> str:
    match = re.search(r"Run ([0-9A-Z]{26}) \(", output)
    assert match, output
    return match.group(1)


def test_us01_run_prints_suite_and_scenario_pass_rates_with_intervals(config_path: Path) -> None:
    code, output = _run(config_path, "--trials", "2")

    assert code == exit_codes.OK, output
    assert "Suite ci: 10 trials over 5 scenarios" in output
    assert re.search(r"pass_rate\s+│\s+100\.0% \[100\.0%, 100\.0%\]\s+│\s+5 │", output), output
    assert "Scenarios (Wilson 95% CI)" in output
    assert output.count("2/2") == 5
    assert output.count("100.0% [34.2%, 100.0%]") == 5  # Wilson interval of 2/2
    assert "10 of 10 trials analyzed · 0 harness errors" in output
    assert "Simulator contradiction rate 0.0% [" in output
    run_id = _run_id(output)
    results_dir = config_path.parent / ".hecklr" / "runs" / run_id
    assert f"Results: {results_dir}" in output
    assert f"Report: {results_dir / 'report.html'}" in output
    assert (results_dir / "results.duckdb").is_file()
    assert "<html" in (results_dir / "report.html").read_text(encoding="utf-8")


def test_failing_judges_lower_the_pass_rates_and_still_exit_zero(config_path: Path) -> None:
    def judge_passes(request: LLMRequest) -> bool:
        return "Saturdays" not in request.messages[-1].content

    code, output = _run(
        config_path, "--trials", "1", overrides=CliOverrides(scripted_models(judge_passes))
    )

    assert code == exit_codes.OK, output
    assert re.search(r"saturday-visits\s+│\s+0/1\s+│\s+0\.0% \[0\.0%, 79\.3%\]", output), output
    store = ResultStore(config_path.parent / ".hecklr" / "runs")
    results = anyio.run(store.load_run, _run_id(output))
    store.close()
    assert len(results.trials) == 5
    assert any(not grade.passed for grade in results.grades)


def test_init_and_run_against_a_real_http_agent(tmp_path: Path, http_agent_url: str) -> None:
    root = tmp_path / "http-project"
    init = runner.invoke(
        app,
        ["init", str(root), "--yes", "--agent-url", http_agent_url, *FAKE_MODEL_FLAGS],
    )
    init_output = click.unstyle(init.output)
    assert init.exit_code == exit_codes.OK, init_output
    assert "using the http adapter" in init_output
    assert "answers in the JSON field reply" in init_output

    code, output = _run(root / "hecklr.yaml", "--trials", "1")

    assert code == exit_codes.OK, output
    # The fixture agent answers but has no tools, so only the FAQ scenario can pass.
    assert re.search(r"saturday-visits\s+│\s+1/1", output), output
    assert re.search(r"reschedule-visit\s+│\s+0/1", output), output
    assert "5 of 5 trials analyzed" in output


def test_channels_option_accepts_commas_and_rejects_unknown_names(config_path: Path) -> None:
    code, _ = _run(config_path, "--trials", "1", "--channels", "chat,")
    assert code == exit_codes.OK

    code, output = _run(config_path, "--channels", "fax")
    assert code == 2
    assert "fax" in output
    assert "known channels:" in output


def test_budget_refusal_has_its_own_exit_code(config_path: Path, edit: Edit) -> None:
    edit(config_path, "simulator: fake/customer", "simulator: anthropic/claude-sonnet-5-5")

    code, output = _run(config_path, "--budget-usd", "0.01")

    assert code == exit_codes.BUDGET
    assert "exceeds the budget of $0.01" in output


def test_config_errors_exit_with_ex_config(config_path: Path) -> None:
    code, output = _run(config_path, "--suite", "nightly")

    assert code == exit_codes.CONFIG_ERROR
    assert "unknown suite 'nightly'" in output


def test_every_trial_failing_to_run_is_a_harness_error(config_path: Path) -> None:
    # Without scripted models, the stock FakeLLM cannot play a customer.
    code, output = _run(config_path, "--trials", "1", overrides=CliOverrides())

    assert code == exit_codes.HARNESS_ERROR
    assert "Every trial failed to run." in output
    assert "Cannot gate: no trial of the candidate run could be analyzed" in output


def test_a_missing_provider_key_is_a_config_error_before_the_run_starts(
    config_path: Path, edit: Edit, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    edit(config_path, "simulator: fake/customer", "simulator: anthropic/claude-haiku-5-5")

    code, output = _run(config_path, "--trials", "1")

    assert code == exit_codes.CONFIG_ERROR, output
    assert "ANTHROPIC_API_KEY" in output
    assert "; run " not in output  # no run id is shown for a run that is never recorded
    assert not (config_path.parent / ".hecklr" / "runs").exists()


def test_a_judge_criterion_model_without_its_key_is_a_config_error(
    config_path: Path, edit: Edit, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    edit(
        config_path.parent / "scenarios" / "saturday-visits.yaml",
        "rubric: tone",
        "rubric: tone\n    model: anthropic/claude-haiku-5-5",
    )

    code, output = _run(config_path, "--trials", "1")

    assert code == exit_codes.CONFIG_ERROR, output
    assert "ANTHROPIC_API_KEY" in output
    assert not (config_path.parent / ".hecklr" / "runs").exists()


def test_a_config_error_raised_mid_run_is_a_harness_error(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def misconfigured(*args: object, **kwargs: object) -> RunResults:
        raise ProviderConfigError("key revoked mid-run")

    monkeypatch.setattr(hecklr.commands.run, "execute_run", misconfigured)

    code, output = _run(config_path, "--trials", "1")

    assert code == exit_codes.HARNESS_ERROR, output
    assert "the run failed: ProviderConfigError: key revoked mid-run" in output


def test_a_crashed_run_is_a_harness_error_naming_every_cause(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def crash(*args: object, **kwargs: object) -> RunResults:
        raise ExceptionGroup("trials", [OSError("disk full"), RuntimeError("driver lost")])

    monkeypatch.setattr(hecklr.commands.run, "execute_run", crash)

    code, output = _run(config_path, "--trials", "1")

    assert code == exit_codes.HARNESS_ERROR
    assert "the run failed: OSError: disk full; RuntimeError: driver lost" in output


def test_baseline_is_recorded_and_seed_is_reproducible(config_path: Path) -> None:
    _, first = _run(config_path, "--trials", "1", "--seed", "5")
    code, second = _run(config_path, "--trials", "1", "--seed", "5", "--baseline", _run_id(first))

    assert code == exit_codes.OK, second
    store = ResultStore(config_path.parent / ".hecklr" / "runs")
    runs: dict[str, Run] = {run.id: run for run in store.list_runs()}
    baseline, candidate = runs[_run_id(first)], runs[_run_id(second)]
    assert candidate.baseline_id == baseline.id
    assert [s.seed for s in candidate.matrix] == [s.seed for s in baseline.matrix]
    # The summary compares with the recorded baseline.
    assert "Verdict: NO CHANGE" in second or "Verdict: INCONCLUSIVE" in second, second
    assert "Comparisons (candidate" in second


def test_adaptive_flag_announces_the_extra_trials(config_path: Path) -> None:
    code, output = _run(config_path, "--trials", "2", "--adaptive")

    assert code == exit_codes.OK, output
    assert "(adaptive)" in output
    assert "Adaptive pass: 5 more trials" in output
    assert "15 of 15 trials analyzed" in output


def test_output_dir_option(config_path: Path, tmp_path: Path) -> None:
    out = tmp_path / "elsewhere"

    code, output = _run(config_path, "--trials", "1", "--output-dir", str(out))

    assert code == exit_codes.OK, output
    assert (out / "runs" / _run_id(output) / "run.json").is_file()


def _results(status: RunStatus) -> RunResults:
    run = Run(
        id=RunId(new_id()),
        created_at=datetime.now(UTC),
        agent=AgentRef(name="a"),
        config_hash="h",
        seed=0,
        matrix=[],
        budget=Budget(usd=Decimal(1)),
        status=status,
    )
    return RunResults(run=run, trials=[], events=[], grades=[])


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (RunStatus.COMPLETED, exit_codes.OK),
        (RunStatus.BUDGET_EXCEEDED, exit_codes.BUDGET),
        (RunStatus.FAILED, exit_codes.HARNESS_ERROR),
        (RunStatus.CANCELLED, exit_codes.HARNESS_ERROR),
    ],
)
def test_run_status_maps_to_exit_code(status: RunStatus, code: int) -> None:
    assert _exit_code(_results(status)) == code
