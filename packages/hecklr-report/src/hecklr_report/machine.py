"""Machine-readable reports: JSON (the full ``Analysis``) and JUnit XML (one test per scenario)."""

import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Final

from hecklr_report import fmt
from hecklr_report.model import Analysis, ScenarioResult

JUNIT_PASS_THRESHOLD: Final = 0.5
"""A scenario's test case fails when most of its trials failed: the same majority rule that
defines a flip, so a red test case and a flipped scenario mean the same thing."""


def render_json(analysis: Analysis) -> str:
    """The full analysis as JSON, carrying ``schema_version``.

    ``Analysis.model_validate_json`` reads it back.
    """
    return analysis.model_dump_json(indent=2)


def render_junit(analysis: Analysis) -> str:
    """JUnit XML with one test case per scenario, readable by any CI.

    A scenario fails when fewer than half its analyzed trials passed; the failure message carries
    the pass rate with its Wilson interval. A scenario with no analyzable trial (all harness
    errors or undecided) is an ``error``.
    """
    suite_name = f"hecklr.{analysis.candidate.suite or analysis.candidate.agent.name}"
    seconds: defaultdict[str, float] = defaultdict(float)
    for trial in analysis.trials:
        seconds[trial.scenario_id] += trial.duration_ms / 1000
    cases = [_testcase(s, suite_name, seconds[s.scenario_id]) for s in analysis.scenarios]
    failures = sum(1 for case in cases if case.find("failure") is not None)
    errors = sum(1 for case in cases if case.find("error") is not None)
    totals = {
        "tests": str(len(cases)),
        "failures": str(failures),
        "errors": str(errors),
        "time": f"{sum(seconds.values()):.3f}",
    }
    root = ET.Element("testsuites", {"name": "hecklr", **totals})
    suite = ET.SubElement(
        root,
        "testsuite",
        {
            "name": suite_name,
            "timestamp": analysis.candidate.created_at.isoformat(),
            "skipped": "0",
            **totals,
        },
    )
    properties = ET.SubElement(suite, "properties")
    for name, value in _properties(analysis).items():
        ET.SubElement(properties, "property", {"name": name, "value": value})
    suite.extend(cases)
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def _properties(analysis: Analysis) -> dict[str, str]:
    properties = {"run_id": analysis.candidate.id, "agent": analysis.candidate.agent.name}
    if analysis.baseline is not None:
        properties["baseline_run_id"] = analysis.baseline.id
    if analysis.verdict is not None:
        properties["verdict"] = analysis.verdict.value
    comparison = analysis.suite_comparison
    if comparison is not None:
        properties["pass_rate_delta"] = fmt.delta(comparison)
    return properties


def _testcase(result: ScenarioResult, classname: str, seconds: float) -> ET.Element:
    case = ET.Element(
        "testcase", {"classname": classname, "name": result.scenario_id, "time": f"{seconds:.3f}"}
    )
    rate = result.pass_rate
    if rate is None:
        ET.SubElement(
            case,
            "error",
            {
                "message": "no trial could be analyzed (harness errors or grade errors)",
                "type": "harness_error",
            },
        )
        return case
    if rate.value >= JUNIT_PASS_THRESHOLD:
        return case
    message = (
        f"pass rate {fmt.estimate(rate, 'percent')} ({fmt.confidence(rate)}, Wilson) "
        f"over {result.trials} trials"
    )
    if result.baseline_pass_rate is not None:
        message += f"; baseline {fmt.estimate(result.baseline_pass_rate, 'percent')}"
    if result.cause is not None:
        message += f"; main cause: {result.cause.cause}"
    failure = ET.SubElement(
        case, "failure", {"message": message, "type": result.flip or "failing_scenario"}
    )
    failure.text = (result.cause.rationale if result.cause else None) or message
    return case
