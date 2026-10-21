"""JSON and JUnit reports, and the terminal summary."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

from rich.console import Console

from hecklr_report import (
    Analysis,
    FailureCause,
    analyze,
    render_json,
    render_junit,
    render_terminal,
)
from hecklr_schemas import Channel, Scenario
from hecklr_stats import JudgeCalibration, wilson_estimate

if TYPE_CHECKING:
    from conftest import Kit
    from hecklr_core import RunResults


def test_json_round_trips_with_schema_version(regression: tuple[Analysis, RunResults]) -> None:
    analysis = regression[0]

    text = render_json(analysis)

    assert json.loads(text)["schema_version"] == 1
    assert Analysis.model_validate_json(text) == analysis


def test_junit_has_one_testcase_per_scenario_with_interval_in_failures(
    regression: tuple[Analysis, RunResults],
) -> None:
    analysis = regression[0]

    root = ET.fromstring(render_junit(analysis))  # noqa: S314 - our own freshly rendered XML

    assert root.tag == "testsuites"
    (suite,) = root.findall("testsuite")
    cases = suite.findall("testcase")
    assert [c.get("name") for c in cases] == [s.scenario_id for s in analysis.scenarios]
    failed = [c for c in cases if c.find("failure") is not None]
    assert suite.get("tests") == str(len(cases)) == root.get("tests")
    assert suite.get("failures") == str(len(failed))
    assert failed
    expected = {
        s.scenario_id for s in analysis.scenarios if s.pass_rate and s.pass_rate.value < 0.5
    }
    assert {c.get("name") for c in failed} == expected
    failure = failed[0].find("failure")
    assert failure is not None
    message = failure.get("message") or ""
    assert message.startswith("pass rate ")
    assert "% [" in message
    assert "(95% CI, Wilson)" in message
    assert failure.get("type") in ("pass_to_fail", "failing_scenario")
    properties = {p.get("name"): p.get("value") for p in suite.iter("property")}
    assert properties["verdict"] == "regression"
    assert all(float(c.get("time") or "-1") >= 0 for c in cases)


def test_junit_marks_unanalyzable_scenarios_as_errors(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    builder = kit.RunBuilder()
    builder.trial("s00", status="harness_error")
    builder.trial("s01", passed=True)

    junit = render_junit(analyze(builder.build(), None, kit.stats, scenarios))
    root = ET.fromstring(junit)  # noqa: S314 - our own freshly rendered XML

    case = root.find("testsuite/testcase[@name='s00']")
    assert case is not None
    error = case.find("error")
    assert error is not None
    assert error.get("type") == "harness_error"
    assert root.get("errors") == "1"


def test_junit_drops_characters_xml_cannot_carry(
    regression: tuple[Analysis, RunResults],
) -> None:
    analysis = regression[0]
    failing = next(s for s in analysis.scenarios if s.pass_rate and s.pass_rate.value < 0.5)
    dirty = failing.model_copy(
        update={"cause": FailureCause(cause="judge:x\x01", rationale="bad\x00 \x08byte\x1f")}
    )
    scenarios = [dirty if s is failing else s for s in analysis.scenarios]

    junit = render_junit(analysis.model_copy(update={"scenarios": scenarios}))
    root = ET.fromstring(junit)  # noqa: S314 - our own freshly rendered XML

    failure = root.find(f"testsuite/testcase[@name='{failing.scenario_id}']/failure")
    assert failure is not None
    assert failure.text == "bad\ufffd \ufffdbyte\ufffd"
    assert "judge:x\ufffd" in (failure.get("message") or "")


def record(analysis: Analysis) -> str:
    console = Console(record=True, width=240, color_system=None)
    render_terminal(analysis, console)
    return console.export_text()


def test_us01_terminal_prints_every_number_with_its_interval(
    regression: tuple[Analysis, RunResults],
) -> None:
    analysis = regression[0]

    text = record(analysis)

    suite = analysis.summary("pass_rate")
    assert suite is not None
    assert (
        f"{100 * suite.value:.1f}% [{100 * suite.ci_low:.1f}%, {100 * suite.ci_high:.1f}%]" in text
    )
    assert "Verdict: REGRESSION" in text
    assert "Comparisons (candidate \N{MINUS SIGN} baseline)" in text
    assert "response_latency_p95" in text
    assert "Scenarios with failures or flips" in text
    assert "more scenarios in the HTML report" in text
    assert "200 of 200 trials analyzed" in text


def test_terminal_prints_user_text_literally(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    builder = kit.RunBuilder()
    builder.trial("s00", passed=False)
    analysis = analyze(builder.build(), None, kit.stats, {})
    scenario = analysis.scenarios[0]
    marked = scenario.model_copy(
        update={"cause": FailureCause(cause="[red]boom[/red]"), "scenario_id": "[b]s00[/b]"}
    )

    text = record(analysis.model_copy(update={"scenarios": [marked]}))

    assert "[red]boom[/red]" in text
    assert "[b]s00[/b]" in text


def test_terminal_voice_judges_and_problems(kit: type[Kit], scenarios: dict[str, Scenario]) -> None:
    personas = {"noisy": kit.persona("noisy", snr_db=5, accent="fr-FR")}
    builder = kit.RunBuilder()
    builder.suite(
        {"s00": 0.5},
        4,
        rng_seed=1,
        channel=Channel.VOICE,
        persona_id="noisy",
        voice=kit.VoiceShape(),
    )
    calibration = JudgeCalibration(
        grader="judge:tone",
        grader_version="v1",
        judge_model=None,
        n=20,
        kappa=wilson_estimate(18, 20),
        precision=None,
        recall=None,
    )
    analysis = analyze(
        builder.build(),
        None,
        kit.stats,
        scenarios,
        personas=personas,
        judge_calibrations=[calibration],
    )
    broken = analysis.model_copy(update={"problems": ["no baseline"]})

    text = record(broken)

    assert "Success by SNR (dB)" in text
    assert "Success by accent" in text
    assert "Voice turn-taking (95% CI)" in text
    assert "all voice" in text
    assert "snr_db=5" in text
    assert "Judge agreement with humans" in text
    assert "yes" in text
    assert "Cannot gate: no baseline" in text
