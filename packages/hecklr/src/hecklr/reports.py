"""Writing an analysis in the spec's file formats (Markdown, HTML, JSON, JUnit)."""

from enum import StrEnum
from pathlib import Path
from typing import Final

from hecklr.results import Analyzed
from hecklr_report import render_html, render_json, render_junit, render_markdown


class ReportFormat(StrEnum):
    """A report file format (the terminal summary is printed, never written)."""

    MD = "md"
    HTML = "html"
    JSON = "json"
    JUNIT = "junit"


FILE_NAMES: Final = {
    ReportFormat.MD: "report.md",
    ReportFormat.HTML: "report.html",
    ReportFormat.JSON: "report.json",
    ReportFormat.JUNIT: "junit.xml",
}
"""Default file name of each format inside the candidate's run directory."""


def write_report(
    analyzed: Analyzed,
    report_format: ReportFormat,
    out_path: Path,
    *,
    run_dir: Path,
    report_url: str | None = None,
) -> Path:
    """Write ``analyzed`` as ``report_format`` to ``out_path`` and return the path.

    Args:
        analyzed: The analysis and its stored runs.
        report_format: Which renderer to use.
        out_path: Destination file; parent directories are created.
        run_dir: The candidate's run directory, which the HTML report's recordings are read from.
        report_url: Where the full report will be published, linked from the Markdown comment.
    """
    if report_format is ReportFormat.HTML:
        return render_html(analyzed.analysis, analyzed.candidate, out_path, run_dir=run_dir)
    match report_format:
        case ReportFormat.MD:
            text = render_markdown(analyzed.analysis, report_url=report_url)
        case ReportFormat.JSON:
            text = render_json(analyzed.analysis)
        case ReportFormat.JUNIT:
            text = render_junit(analyzed.analysis)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return out_path
