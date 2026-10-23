"""``hecklr bench`` end to end: a real agent process, scripted models, rows and the site (US-12)."""

import json
import re
from pathlib import Path

import click
import pytest
from typer.testing import CliRunner

from hecklr import exit_codes
from hecklr.cli import app
from hecklr.commands._common import CliOverrides
from hecklr.commands.bench.result import StackResult
from hecklr.testing import scripted_models
from hecklr_llm import LLMOutputError
from hecklr_schemas import Estimate, IntervalMethod, LLMRequest, LLMResponse

from .conftest import write_stack

# The agent under test and the world sandbox listen on 127.0.0.1 ephemeral ports.
pytestmark = [pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]

runner = CliRunner()


def _bench(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["bench", *args], obj=CliOverrides(scripted_models()))
    return result.exit_code, click.unstyle(result.output)


def _run(bench_dir: Path, tmp_path: Path, *args: str) -> tuple[int, str]:
    return _bench("run", "--bench-dir", str(bench_dir), "--work-dir", str(tmp_path / "work"), *args)


def _row(bench_dir: Path, stack: str = "acme-rules") -> StackResult:
    return StackResult.model_validate_json((bench_dir / "results" / f"{stack}.json").read_bytes())


def test_us12_run_writes_a_row_with_every_metric_and_its_interval(
    bench_dir: Path, tmp_path: Path
) -> None:
    code, output = _run(bench_dir, tmp_path, "--stack", "acme-rules")

    assert code == exit_codes.OK, output
    row = _row(bench_dir)
    assert row.benchmark_version == "test-1"
    assert row.seed == 7
    assert (row.scenarios, row.trials_per_scenario, row.counts.total) == (2, 2, 4)
    assert row.counts.harness_error == 0
    expected = {
        "task_success",
        "judge:no_hallucination",
        "judge:resolved",
        "response_latency_p50",
        "response_latency_p95",
        "cost_per_conversation_usd",
    }
    assert expected <= row.metrics.keys()
    for estimate in row.metrics.values():
        assert estimate.ci_low <= estimate.value <= estimate.ci_high
    # Scripted models and the rules brain cost nothing, and the agent said so.
    assert row.spend.total_usd == 0
    assert row.metrics["cost_per_conversation_usd"].value == 0
    assert row.environment.packages
    assert "Wrote" in output
    assert re.search(r"task_success\s+│\s+\d+\.\d% \[", output), output


def test_us12_reproduce_checks_every_metric_against_the_published_row(
    bench_dir: Path, tmp_path: Path
) -> None:
    assert _run(bench_dir, tmp_path, "--stack", "acme-rules")[0] == exit_codes.OK

    code, output = _bench(
        "reproduce", "--stack", "acme-rules", "--bench-dir", str(bench_dir),
        "--work-dir", str(tmp_path / "work"),
    )  # fmt: skip

    assert code == exit_codes.OK, output
    assert "REPRODUCED" in output
    assert (tmp_path / "work" / "acme-rules.reproduction.json").is_file()


def test_us12_reproduce_fails_when_a_published_interval_is_not_met(
    bench_dir: Path, tmp_path: Path
) -> None:
    assert _run(bench_dir, tmp_path, "--stack", "acme-rules")[0] == exit_codes.OK
    path = bench_dir / "results" / "acme-rules.json"
    row = _row(bench_dir)
    impossible = Estimate(value=1e6, ci_low=1e6, ci_high=2e6, n=4, method=IntervalMethod.BOOTSTRAP)
    tampered = row.model_copy(
        update={"metrics": {**row.metrics, "response_latency_p50": impossible}}
    )
    path.write_text(tampered.model_dump_json(), encoding="utf-8")

    code, output = _bench(
        "reproduce", "--stack", "acme-rules", "--bench-dir", str(bench_dir),
        "--work-dir", str(tmp_path / "work"),
    )  # fmt: skip

    assert code == exit_codes.CHECK_FAILED, output
    assert "NOT REPRODUCED: response_latency_p50" in output


def test_reproduce_refuses_a_row_of_a_changed_suite(bench_dir: Path, tmp_path: Path) -> None:
    assert _run(bench_dir, tmp_path, "--stack", "acme-rules")[0] == exit_codes.OK
    scenario = bench_dir / "suite" / "scenarios" / "chat" / "invoice-amount-due.yaml"
    scenario.write_text(scenario.read_text("utf-8").replace("max_turns: 20", "max_turns: 19"))

    code, output = _bench("reproduce", "--stack", "acme-rules", "--bench-dir", str(bench_dir))

    assert code == exit_codes.CONFIG_ERROR
    assert "suite digest differs" in output


def test_reproduce_needs_a_published_row(bench_dir: Path) -> None:
    code, output = _bench("reproduce", "--stack", "acme-rules", "--bench-dir", str(bench_dir))

    assert code == exit_codes.CONFIG_ERROR
    assert "no published row" in output


def test_site_renders_the_rows_into_a_page_that_opens_from_disk(
    bench_dir: Path, tmp_path: Path
) -> None:
    assert _run(bench_dir, tmp_path, "--stack", "acme-rules")[0] == exit_codes.OK

    code, output = _bench("site", "--bench-dir", str(bench_dir))

    assert code == exit_codes.OK, output
    dist = bench_dir / "site" / "dist"
    assert {"index.html", "app.js", "style.css", "data.js"} <= {p.name for p in dist.iterdir()}
    script = (dist / "data.js").read_text("utf-8")
    data = json.loads(script.removeprefix("window.HECKLR_BENCH = ").removesuffix(";\n"))
    assert data["benchmark_version"] == "test-1"
    assert [row["stack"]["id"] for row in data["rows"]] == ["acme-rules"]
    assert '<script src="data.js">' in (dist / "index.html").read_text("utf-8")
    assert "1 rows" in output


def test_a_stack_that_cannot_start_is_reported_and_the_others_still_run(
    bench_dir: Path, tmp_path: Path
) -> None:
    write_stack(
        bench_dir,
        "broken",
        """\
        id: broken
        name: An agent that exits at once
        track: chat
        description: Exits with code 3.
        why: Tests the runner.
        components: {brain: none}
        command: ["{python}", "-c", "raise SystemExit(3)"]
        ready: {http: "http://127.0.0.1:{port}/healthz", timeout_s: 30}
        adapter: {type: http, url: "http://127.0.0.1:{port}/chat"}
        budget_usd: 0
        """,
    )

    code, output = _run(bench_dir, tmp_path, "--all")

    assert code == exit_codes.HARNESS_ERROR
    assert "broken not measured: the agent exited with code 3" in output
    assert (bench_dir / "results" / "acme-rules.json").is_file()
    assert not (bench_dir / "results" / "broken.json").exists()


def test_a_stack_missing_its_api_key_is_not_started(
    bench_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HECKLR_BENCH_TEST_KEY", raising=False)
    write_stack(
        bench_dir,
        "keyed",
        """\
        id: keyed
        name: Needs a key
        track: chat
        description: d
        why: w
        components: {brain: cloud}
        command: ["{python}", "-c", "raise SystemExit(0)"]
        requires_env: [HECKLR_BENCH_TEST_KEY]
        ready: {line: "never"}
        adapter: {type: http, url: "http://127.0.0.1:{port}/chat"}
        budget_usd: 0
        """,
    )

    code, output = _run(bench_dir, tmp_path, "--stack", "keyed")

    assert code == exit_codes.HARNESS_ERROR
    assert "keyed needs HECKLR_BENCH_TEST_KEY set in the environment" in output


def test_run_refuses_stacks_whose_caps_exceed_the_budget(bench_dir: Path, tmp_path: Path) -> None:
    code, output = _run(bench_dir, tmp_path, "--all", "--budget-usd", "0.001")

    assert code == exit_codes.BUDGET
    assert "may spend up to $0.01" in output


def test_run_needs_a_stack(bench_dir: Path, tmp_path: Path) -> None:
    code, output = _run(bench_dir, tmp_path)

    assert code == exit_codes.CONFIG_ERROR
    assert "name a stack with --stack" in output


def test_an_unknown_stack_is_a_config_error(bench_dir: Path, tmp_path: Path) -> None:
    code, output = _run(bench_dir, tmp_path, "--stack", "nope")

    assert code == exit_codes.CONFIG_ERROR
    assert "unknown stack ['nope']; known: acme-rules" in output


def test_list_shows_stacks_their_needs_and_whether_they_are_published(bench_dir: Path) -> None:
    code, output = _bench("list", "--bench-dir", str(bench_dir))

    assert code == exit_codes.OK, output
    assert "Hecklr benchmark test-1" in output
    assert re.search(r"acme-rules\s+│\s+chat\s+│\s+nothing\s+│\s+0\.01\s+│\s+no", output), output


def test_commands_name_a_missing_manifest(tmp_path: Path) -> None:
    code, output = _bench("list", "--bench-dir", str(tmp_path))

    assert code == exit_codes.CONFIG_ERROR
    assert "no benchmark manifest here" in output


class _BrokenJudge:
    """The scripted customer, and a judge whose every answer is malformed."""

    def __init__(self) -> None:
        self._scripted = scripted_models()["fake"]

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if request.schema_name == "JudgeVerdict":
            raise LLMOutputError("the judge answered something that is not a verdict")
        return await self._scripted.complete(request)


def test_a_row_with_conversations_left_without_a_verdict_is_flagged(
    bench_dir: Path, tmp_path: Path
) -> None:
    result = runner.invoke(
        app,
        ["bench", "run", "--stack", "acme-rules", "--bench-dir", str(bench_dir),
         "--work-dir", str(tmp_path / "work")],
        obj=CliOverrides({"fake": _BrokenJudge()}),
    )  # fmt: skip

    output = click.unstyle(result.output)
    assert result.exit_code == exit_codes.OK, output
    assert re.search(r"warning: \d of 4 conversations have no verdict \(\d+ grade errors", output)
