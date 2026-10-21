"""``hecklr compare``, ``hecklr gate`` and ``hecklr report`` on runs in a real result store."""

import json
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

import click
import pytest
from typer.testing import CliRunner

from hecklr import exit_codes
from hecklr.cli import app
from hecklr_report import EXIT_HARNESS_ERROR, EXIT_INCONCLUSIVE, EXIT_PASS, EXIT_REGRESSION
from hecklr_schemas import Run, RunStatus

Edit = Callable[[Path, str, str], None]
StoreRun = Callable[..., Run]

runner = CliRunner()
SCENARIOS = 30


def rates(passing: float, *, failing_share: float = 0.0) -> dict[str, float]:
    """``SCENARIOS`` scenarios passing with ``passing``; the first ``failing_share`` never pass."""
    failing = round(failing_share * SCENARIOS)
    return {f"s{i:02d}": 0.0 if i < failing else passing for i in range(SCENARIOS)}


@pytest.fixture(autouse=True)
def fast_stats(config_path: Path, edit: Edit) -> None:
    """Fewer bootstrap resamples and a fixed seed keep the analysis fast and deterministic."""
    edit(
        config_path,
        "stats: { alpha: 0.05, min_effect: 0.03 }",
        "stats: { alpha: 0.05, min_effect: 0.03, bootstrap_resamples: 1000, seed: 3 }",
    )


def invoke(config_path: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, [*args, "--config", str(config_path)])
    return result.exit_code, click.unstyle(result.output)


def flat(output: str) -> str:
    """``output`` with Rich's line wrapping and table borders undone, for substring checks."""
    return " ".join(output.replace("│", " ").split())


# --- gate -------------------------------------------------------------------------------------


def test_us04_gate_exits_1_on_a_significant_regression(
    config_path: Path, store: StoreRun, runs_dir: Path
) -> None:
    store(rates(1.0), order=0)
    candidate = store(rates(1.0, failing_share=0.5), order=1)

    code, output = invoke(config_path, "gate")

    assert code == EXIT_REGRESSION, output
    assert output.startswith("### Hecklr · REGRESSION")
    assert "Flipped pass → fail: 15 scenarios" in output
    assert "Gate: REGRESSION (exit 1)" in output
    run_dir = runs_dir / candidate.id
    for name in ("report.md", "report.html", "report.json", "junit.xml"):
        assert (run_dir / name).is_file(), name


def test_us05_gate_comment_stays_under_30_lines(config_path: Path, store: StoreRun) -> None:
    store(rates(1.0), order=0)
    store(rates(1.0, failing_share=0.5), order=1)

    _, output = invoke(config_path, "gate")

    comment = output.split("Gate: ")[0]
    assert len(comment.strip().splitlines()) < 30


def test_us04_gate_passes_an_unchanged_agent(config_path: Path, store: StoreRun) -> None:
    store(rates(1.0), order=0)
    store(rates(1.0), order=1)

    code, output = invoke(config_path, "gate")

    assert code == EXIT_PASS, output
    assert "NO CHANGE" in output


def test_us04_a_drop_below_the_minimum_effect_is_not_a_regression(
    config_path: Path, store: StoreRun
) -> None:
    store(rates(1.0), order=0)
    store(rates(1.0, failing_share=1 / SCENARIOS), order=1)  # one scenario: 3.3 points, p > 0.05

    code, output = invoke(config_path, "gate")

    assert code in {EXIT_PASS, EXIT_INCONCLUSIVE}, output
    assert "REGRESSION" not in output


def test_gate_inconclusive_exit_code_is_configurable(
    config_path: Path, store: StoreRun, edit: Edit
) -> None:
    # Five scenarios can never reach p < 0.05 (sign-flip test), so a big drop is inconclusive.
    store({f"s{i}": 1.0 for i in range(5)}, order=0)
    store({f"s{i}": 0.0 if i < 2 else 1.0 for i in range(5)}, order=1)

    code, output = invoke(config_path, "gate")
    assert code == EXIT_INCONCLUSIVE, output

    code, output = invoke(config_path, "gate", "--on-inconclusive", "pass")
    assert code == EXIT_PASS, output

    edit(config_path, "budget:", "gate: { on_inconclusive: pass }\nbudget:")
    code, output = invoke(config_path, "gate")
    assert code == EXIT_PASS, output


def test_gate_uses_the_recorded_baseline_over_the_previous_run(
    config_path: Path, store: StoreRun, runs_dir: Path
) -> None:
    good = store(rates(1.0), order=0)
    store(rates(1.0, failing_share=0.5), order=1)
    candidate = store(rates(1.0), order=2, baseline_id=good.id)

    code, output = invoke(config_path, "gate")

    assert code == EXIT_PASS, output
    report = json.loads((runs_dir / candidate.id / "report.json").read_text("utf-8"))
    assert report["baseline"]["id"] == good.id


def test_gate_takes_explicit_run_ids_and_a_suite(config_path: Path, store: StoreRun) -> None:
    base = store(rates(1.0), order=0)
    bad = store(rates(1.0, failing_share=0.5), order=1)
    store(rates(1.0), order=2, suite="nightly")

    code, output = invoke(
        config_path, "gate", "--candidate", bad.id, "--baseline", base.id, "--suite", "ci"
    )
    assert code == EXIT_REGRESSION, output

    code, output = invoke(config_path, "gate", "--suite", "ci")
    assert code == EXIT_REGRESSION, output


def test_gate_on_a_failed_candidate_run_is_a_harness_error(
    config_path: Path, store: StoreRun
) -> None:
    store(rates(1.0), order=0)
    store(rates(1.0), order=1, status=RunStatus.FAILED)

    code, output = invoke(config_path, "gate")

    assert code == EXIT_HARNESS_ERROR, output
    assert "is failed" in output
    assert "Gate: HARNESS ERROR (exit 3)" in output


def test_gate_without_any_run_or_with_unknown_ids_is_a_harness_error(
    config_path: Path, store: StoreRun
) -> None:
    code, output = invoke(config_path, "gate")
    assert code == EXIT_HARNESS_ERROR
    assert "no finished run" in output

    run = store(rates(1.0), order=0)
    code, output = invoke(config_path, "gate", "--baseline", "01J00000000000000000000000")
    assert code == EXIT_HARNESS_ERROR
    assert "no stored run '01J00000000000000000000000'" in output

    code, output = invoke(config_path, "gate", "--candidate", "yesterday")
    assert code == EXIT_HARNESS_ERROR
    assert "neither a run id" in output
    assert run.id not in output


def test_gate_with_a_broken_config_is_a_harness_error(config_path: Path, edit: Edit) -> None:
    edit(config_path, "version: 1", "version: 1\nunknown_key: 2")

    code, output = invoke(config_path, "gate")

    assert code == EXIT_HARNESS_ERROR
    assert "unknown_key" in output


def test_a_first_run_without_baseline_fails_or_passes_as_asked(
    config_path: Path, store: StoreRun, tmp_path: Path
) -> None:
    store(rates(1.0), order=0)

    code, output = invoke(config_path, "gate")
    assert code == EXIT_HARNESS_ERROR
    assert "No baseline run" in output

    out = tmp_path / "out"
    code, output = invoke(config_path, "gate", "--missing-baseline", "pass", "--out-dir", str(out))
    assert code == EXIT_PASS, output
    assert "nothing to compare with, so the gate passes" in flat(output)
    assert (out / "report.md").is_file()


def test_gate_links_the_full_report(config_path: Path, store: StoreRun, tmp_path: Path) -> None:
    store(rates(1.0), order=0)
    store(rates(1.0), order=1)
    out = tmp_path / "gate"

    code, output = invoke(
        config_path, "gate", "--report-url", "https://ci.example/run/7", "--out-dir", str(out)
    )

    assert code == EXIT_PASS
    assert "[Full report](https://ci.example/run/7)" in output
    assert "[Full report](https://ci.example/run/7)" in (out / "report.md").read_text("utf-8")


# --- compare ----------------------------------------------------------------------------------


def test_compare_prints_the_comparison_with_intervals(config_path: Path, store: StoreRun) -> None:
    base = store(rates(1.0), order=0)
    cand = store(rates(1.0, failing_share=0.5), order=1)

    code, output = invoke(config_path, "compare", base.id, cand.id)

    assert code == exit_codes.OK, output
    assert "Verdict: REGRESSION" in output
    assert "Comparisons (candidate" in output
    assert "\N{MINUS SIGN}50.0" in flat(output)


def test_compare_baseline_alias_and_latest_default(config_path: Path, store: StoreRun) -> None:
    store(rates(1.0), order=0)
    store(rates(1.0), order=1)

    code, output = invoke(config_path, "compare", "baseline")

    assert code == exit_codes.OK, output
    assert "Verdict: NO CHANGE" in output


@pytest.mark.parametrize("output_format", ["md", "json", "junit"])
def test_compare_writes_machine_formats_to_stdout(
    config_path: Path, store: StoreRun, output_format: str
) -> None:
    base = store(rates(1.0), order=0)
    cand = store(rates(1.0, failing_share=0.5), order=1)

    code, output = invoke(config_path, "compare", base.id, cand.id, "--format", output_format)

    assert code == exit_codes.OK, output
    if output_format == "json":
        assert json.loads(output)["verdict"] == "regression"
    elif output_format == "junit":
        # Hecklr's own renderer wrote this XML; nothing untrusted is parsed.
        assert ET.fromstring(output).tag in {"testsuites", "testsuite"}  # noqa: S314
    else:
        assert output.startswith("### Hecklr · REGRESSION")


def test_compare_writes_html_to_the_run_directory_or_out(
    config_path: Path, store: StoreRun, runs_dir: Path, tmp_path: Path
) -> None:
    base = store(rates(1.0), order=0)
    cand = store(rates(1.0), order=1)

    code, output = invoke(config_path, "compare", base.id, cand.id, "--format", "html")
    assert code == exit_codes.OK, output
    assert (runs_dir / cand.id / "report.html").is_file()

    out = tmp_path / "c.json"
    code, output = invoke(config_path, "compare", base.id, cand.id, "-f", "json", "--out", str(out))
    assert code == exit_codes.OK, output
    assert f"Wrote {out}" in output.replace("\n", "")
    assert json.loads(out.read_text("utf-8"))["candidate"]["id"] == cand.id


def test_compare_without_an_earlier_run_is_a_usage_error(
    config_path: Path, store: StoreRun
) -> None:
    store(rates(1.0), order=0)

    code, output = invoke(config_path, "compare", "baseline")

    assert code == 2
    assert "no earlier completed run" in flat(output)


def test_compare_with_an_unknown_run_is_a_config_error(config_path: Path) -> None:
    code, output = invoke(config_path, "compare", "01J00000000000000000000000", "latest")

    assert code == exit_codes.CONFIG_ERROR
    assert "no finished run" in output


# --- report -----------------------------------------------------------------------------------


def test_report_writes_html_for_the_latest_run_and_prints_its_path(
    config_path: Path, store: StoreRun, runs_dir: Path
) -> None:
    run = store(rates(1.0), order=0)

    code, output = invoke(config_path, "report")

    path = runs_dir / run.id / "report.html"
    assert code == exit_codes.OK, output
    assert str(path) in output.replace("\n", "")
    assert "<html" in path.read_text("utf-8")


def test_report_writes_every_requested_format_and_compares_with_a_baseline(
    config_path: Path, store: StoreRun, tmp_path: Path
) -> None:
    base = store(rates(1.0), order=0)
    run = store(rates(1.0, failing_share=0.5), order=1)
    out = tmp_path / "reports"

    code, output = invoke(
        config_path,
        "report",
        run.id,
        "-f", "md", "-f", "json", "-f", "junit", "-f", "html",
        "--baseline", base.id,
        "--out-dir", str(out),
    )  # fmt: skip

    assert code == exit_codes.OK, output
    assert sorted(p.name for p in out.iterdir()) == [
        "junit.xml",
        "report.html",
        "report.json",
        "report.md",
    ]
    assert json.loads((out / "report.json").read_text("utf-8"))["verdict"] == "regression"


def test_report_uses_the_recorded_baseline(config_path: Path, store: StoreRun) -> None:
    base = store(rates(1.0), order=0)
    store(rates(1.0), order=1, baseline_id=base.id)

    code, output = invoke(config_path, "report", "-f", "json")

    assert code == exit_codes.OK, output
    report = Path(output.strip().replace("\n", ""))
    assert json.loads(report.read_text("utf-8"))["baseline"]["id"] == base.id


def test_report_open_launches_a_browser(
    config_path: Path, store: StoreRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    store(rates(1.0), order=0)
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)

    code, output = invoke(config_path, "report", "--open")

    assert code == exit_codes.OK, output
    assert len(opened) == 1
    assert opened[0].startswith("file://")
    assert opened[0].endswith("report.html")


def test_report_open_needs_html(config_path: Path, store: StoreRun) -> None:
    store(rates(1.0), order=0)

    code, output = invoke(config_path, "report", "--open", "-f", "md")

    assert code == exit_codes.CONFIG_ERROR
    assert "--open needs the html format" in output


def test_report_on_an_empty_store_is_a_config_error(config_path: Path) -> None:
    code, output = invoke(config_path, "report")

    assert code == exit_codes.CONFIG_ERROR
    assert "no finished run" in output
