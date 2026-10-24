"""``hecklr run`` through Typer's CliRunner, on starter projects and scripted models."""

import asyncio
import logging
import os
import re
import signal
import subprocess
import sys
import time
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
from hecklr_llm import FakeLLM, ProviderConfigError
from hecklr_schemas import AgentRef, Budget, LLMRequest, Run, RunId, RunStatus, new_id

Edit = Callable[[Path, str, str], None]
StubAgent = Callable[..., str]

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
    # Wilson on 5 all-pass scenarios, not a collapsed bootstrap (issue #14, ADR 0005c).
    assert re.search(r"pass_rate\s+│\s+100\.0% \[56\.6%, 100\.0%\]\s+│\s+5 │", output), output
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


SCRIPTED = """\
id: scripted-saturday
title: Scripted Saturday question
channels: [chat]
persona: polite-regular
world: acme-starter
mode: scripted
goal: Ask about Saturday visits.
user_turns: ["Do you do maintenance visits on Saturdays?"]
success:
  - type: not_contains
    text: traceback
"""


def test_issue36_budget_zero_runs_a_scripted_judge_free_suite_with_paid_models(
    config_path: Path, edit: Edit
) -> None:
    for role in ("simulator: fake/customer", "judge: fake/judge"):
        edit(config_path, role, f"{role.split(':')[0]}: anthropic/claude-sonnet-5-5")
    edit(
        config_path,
        "suites:\n",
        'suites:\n  smoke: { scenarios: "scripted/*.yaml", trials: 2, channels: [chat] }\n',
    )
    (config_path.parent / "scripted").mkdir()
    (config_path.parent / "scripted" / "saturday.yaml").write_text(SCRIPTED, encoding="utf-8")

    # No provider is injected: the suite calls no model, so it needs no key and costs nothing.
    result = runner.invoke(
        app, ["run", "--config", str(config_path), "--suite", "smoke", "--budget-usd", "0"]
    )
    output = click.unstyle(result.output)

    assert result.exit_code == exit_codes.OK, output
    assert "Suite smoke: 2 trials over 1 scenarios, estimated cost at most $0.00" in output
    assert "Spent: $0.0000 of budget $0.00" in output


def test_config_errors_exit_with_ex_config(config_path: Path) -> None:
    code, output = _run(config_path, "--suite", "nightly")

    assert code == exit_codes.CONFIG_ERROR
    assert "unknown suite 'nightly'" in output


def test_every_trial_failing_to_run_is_a_harness_error(config_path: Path) -> None:
    # A stock FakeLLM cannot play a customer.
    code, output = _run(config_path, "--trials", "1", overrides=CliOverrides({"fake": FakeLLM()}))

    assert code == exit_codes.HARNESS_ERROR
    assert "Every trial failed to run." in output
    assert "Cannot gate: no trial of the candidate run could be analyzed" in output


def test_issue33_a_run_where_no_trial_got_through_exits_3_and_fails_a_first_gate(
    config_path: Path, stub_agent: StubAgent
) -> None:
    stub_agent("failing")

    code, output = _run(config_path, "--trials", "1")

    assert code == exit_codes.HARNESS_ERROR, output
    assert "No trial got through: 5 agent errors, first 'agent returned HTTP 500: boom'" in output
    assert f"Run {_run_id(output)} (completed)" in output
    gate = runner.invoke(app, ["gate", "--config", str(config_path), "--missing-baseline", "pass"])
    gate_output = click.unstyle(gate.output)
    assert gate.exit_code == exit_codes.HARNESS_ERROR, gate_output
    assert "no trial of the candidate run got through" in gate_output


def test_issue34_a_flooding_agent_keeps_the_store_and_the_report_small(
    config_path: Path, stub_agent: StubAgent
) -> None:
    # The red team's repro: 20 MB per reply made a 2.9 GB store and a 3.0 GB report.
    stub_agent("flooding", 20_000_000)

    code, output = _run(config_path, "--trials", "1")

    assert code == exit_codes.HARNESS_ERROR, output
    assert "5 agent errors, first 'agent reply over 262144 bytes" in output
    run_dir = config_path.parent / ".hecklr" / "runs" / _run_id(output)
    stored = sum(path.stat().st_size for path in run_dir.iterdir())
    assert stored < 8_000_000, sorted((p.name, p.stat().st_size) for p in run_dir.iterdir())
    report = (run_dir / "report.html").read_text(encoding="utf-8")
    assert len(report) < 500_000
    assert "truncated, 252144 more characters" in report


def test_issue34_max_reply_bytes_sets_the_cap(config_path: Path, stub_agent: StubAgent) -> None:
    stub_agent("flooding", 2_000)

    code, output = _run(config_path, "--trials", "1", "--max-reply-bytes", "1000")
    assert code == exit_codes.HARNESS_ERROR, output
    assert "agent reply over 1000 bytes" in output

    code, output = _run(config_path, "--trials", "1", "--max-reply-bytes", "2000")
    assert code == exit_codes.OK, output


@pytest.mark.slow
def test_issue35_ctrl_c_prints_one_line_and_exits_130(
    config_path: Path, stub_agent: StubAgent
) -> None:
    stub_agent("slow", 2.0)
    # A real process: SIGINT must reach asyncio's own handler, as it does in a terminal.
    command = [sys.executable, "-c", "from hecklr.cli import main; main()", "run"]
    with subprocess.Popen(  # noqa: S603 -- this interpreter and a test-made config path
        [*command, "-c", str(config_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # CI forces colour on; the assertions read plain text.
        env={k: v for k, v in os.environ.items() if k != "FORCE_COLOR"} | {"NO_COLOR": "1"},
    ) as process:
        try:
            assert process.stderr is not None
            plan = click.unstyle(process.stderr.readline())
            assert plan.startswith("Suite ci:"), plan
            run_id = plan.split("; run ")[1].strip()
            recorded = config_path.parent / ".hecklr" / "runs" / run_id / "run.json"
            deadline = time.monotonic() + 30
            while not recorded.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(0.5)  # trials are in flight: each agent reply takes 2 s
            process.send_signal(signal.SIGINT)
            _, rest = process.communicate(timeout=60)
        finally:
            if process.poll() is None:
                process.kill()

    assert process.returncode == exit_codes.INTERRUPTED, rest
    assert click.unstyle(rest).strip() == f"Interrupted: run {run_id} recorded as cancelled"
    store = ResultStore(config_path.parent / ".hecklr" / "runs")
    (run,) = store.list_runs()
    store.close()
    assert (run.id, run.status) == (run_id, RunStatus.CANCELLED)


def test_issue35_only_cancellations_of_the_embedded_server_are_silenced(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("uvicorn.error")
    try:
        raise asyncio.CancelledError
    except asyncio.CancelledError as exc:
        cancelled = exc
    with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
        with hecklr.commands.run._quiet_server_cancellations():
            logger.error("Exception in 'lifespan' protocol", exc_info=cancelled)
            logger.error("Traceback ...\nasyncio.exceptions.CancelledError\n")
            logger.error("Exception in ASGI application", exc_info=ValueError("tool bug"))
        logger.error("after the run")

    assert [r.getMessage() for r in caplog.records] == [
        "Exception in ASGI application",
        "after the run",
    ]


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


SCRIPTED_SCENARIO = """\
id: scripted-appointment
title: Scripted appointment lookup
channels: [chat]
persona: polite-regular
world: acme-starter
mode: scripted
goal: Find out when the visit is.
user_turns: ["Hi, when is the boiler visit of customer C-1001?"]
success:
  - type: tool_called
    tool: get_appointment
limits: { max_turns: 4 }
"""


def _keyless_scripted_suite(config_path: Path, edit: Edit, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    edit(config_path, "simulator: fake/customer", "simulator: openai/gpt-6-luna")
    edit(config_path, "judge: fake/judge", "judge: openai/gpt-6-luna")
    smoke = '  smoke: { scenarios: "scripted/*.yaml", trials: 1 }\n'
    edit(config_path, "suites:\n", f"suites:\n{smoke}")
    scenario = config_path.parent / "scripted" / "lookup.yaml"
    scenario.parent.mkdir()
    scenario.write_text(SCRIPTED_SCENARIO)
    return scenario


def test_issue19_a_suite_that_calls_no_model_needs_no_key(
    config_path: Path, edit: Edit, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keyless_scripted_suite(config_path, edit, monkeypatch)

    code, output = _run(config_path, "--suite", "smoke", overrides=CliOverrides())

    assert code == exit_codes.OK, output
    assert "Spent: $0.0000" in output


def test_issue19_a_judge_criterion_still_needs_the_judge_key(
    config_path: Path, edit: Edit, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _keyless_scripted_suite(config_path, edit, monkeypatch)
    edit(scenario, "limits:", "  - type: judge\n    rubric: resolved\nlimits:")

    code, output = _run(config_path, "--suite", "smoke", overrides=CliOverrides())

    assert code == exit_codes.CONFIG_ERROR, output
    assert "OPENAI_API_KEY" in output


def test_issue19_a_guided_scenario_still_needs_the_simulator_key(
    config_path: Path, edit: Edit, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keyless_scripted_suite(config_path, edit, monkeypatch)

    code, output = _run(config_path, "--suite", "ci", "--trials", "1", overrides=CliOverrides())

    assert code == exit_codes.CONFIG_ERROR, output
    assert "OPENAI_API_KEY" in output


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


_ISSUE112_AGENT = """\
import os

import hecklr
import hecklr_agent


@hecklr.agent(channel="chat")
async def reply(session: hecklr.Session, message: str) -> str:
    text = await hecklr_agent.reply(session, message)
    if os.environ.get("ISSUE112_BROKEN") and "saturday" in message.lower():
        return "Sure \\ud83d"  # an emoji escape cut in half, as json.loads decodes it
    return text
"""


def test_issue112_a_reply_with_a_lone_surrogate_is_an_agent_error_and_fails_the_gate(
    config_path: Path, edit: Callable[[Path, str, str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    (config_path.parent / "issue112_agent.py").write_text(_ISSUE112_AGENT, encoding="utf-8")
    edit(config_path, "hecklr_agent:reply", "issue112_agent:reply")
    code, output = _run(config_path)
    assert code == exit_codes.OK, output

    monkeypatch.setenv("ISSUE112_BROKEN", "1")
    code, output = _run(config_path)

    assert code == exit_codes.OK, output
    flat = " ".join(output.split())
    assert "15 of 15 trials analyzed · 0 harness errors" in flat
    assert "3 agent errors" in flat
    assert "saturday-visits │ 0/3" in flat
    gate = runner.invoke(app, ["gate", "--config", str(config_path)])
    assert gate.exit_code != exit_codes.OK, click.unstyle(gate.output)
