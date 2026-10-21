"""Hecklr reports: terminal, Markdown, HTML, JSON and JUnit.

``analyze`` turns stored results into an ``Analysis`` once; every renderer only formats it::

    analysis = analyze(candidate, baseline, config.stats, scenarios, personas=personas)
    render_terminal(analysis, console)
    comment = render_markdown(analysis, report_url=url)
    render_html(analysis, candidate, run_dir / "report.html")
    exit_code = gate_exit_code(analysis, config.gate)
"""

from hecklr_report.analysis import COST_PER_TRIAL, analyze
from hecklr_report.gate import (
    EXIT_HARNESS_ERROR,
    EXIT_INCONCLUSIVE,
    EXIT_PASS,
    EXIT_REGRESSION,
    gate_exit_code,
)
from hecklr_report.html import render_html
from hecklr_report.machine import render_json, render_junit
from hecklr_report.markdown import render_markdown
from hecklr_report.model import (
    Analysis,
    DegradationPoint,
    FailureCause,
    FlipGroup,
    JudgeTrust,
    RunInfo,
    ScenarioResult,
    TrialCounts,
    TrialRow,
    VoiceSection,
)
from hecklr_report.terminal import render_terminal

__version__ = "0.1.0"

__all__ = [
    "COST_PER_TRIAL",
    "EXIT_HARNESS_ERROR",
    "EXIT_INCONCLUSIVE",
    "EXIT_PASS",
    "EXIT_REGRESSION",
    "Analysis",
    "DegradationPoint",
    "FailureCause",
    "FlipGroup",
    "JudgeTrust",
    "RunInfo",
    "ScenarioResult",
    "TrialCounts",
    "TrialRow",
    "VoiceSection",
    "__version__",
    "analyze",
    "gate_exit_code",
    "render_html",
    "render_json",
    "render_junit",
    "render_markdown",
    "render_terminal",
]
