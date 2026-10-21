"""Hecklr reports: terminal, Markdown, HTML, JSON and JUnit."""

from hecklr_report.analysis import COST_PER_TRIAL, analyze
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

__version__ = "0.1.0"

__all__ = [
    "COST_PER_TRIAL",
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
]
