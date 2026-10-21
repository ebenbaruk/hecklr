"""``hecklr compare BASELINE CANDIDATE``: the statistical comparison of two stored runs."""

from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.markup import escape

from hecklr.commands._common import (
    DEFAULT_CONFIG,
    ConfigPath,
    config_errors,
    load_project_or_exit,
    stdout,
)
from hecklr.reports import FILE_NAMES, ReportFormat, write_report
from hecklr.results import analyze_runs, open_store, resolve_baseline, resolve_run, runs_dir
from hecklr_report import render_json, render_junit, render_markdown, render_terminal

OutputFormat = Literal["terminal", "md", "html", "json", "junit"]


def compare(
    baseline: Annotated[
        str, typer.Argument(help="Baseline run: an id, `latest` or `baseline` (of the candidate).")
    ],
    candidate: Annotated[str, typer.Argument(help="Candidate run: an id or `latest`.")] = "latest",
    config: ConfigPath = DEFAULT_CONFIG,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Output format.")
    ] = "terminal",
    out: Annotated[
        Path | None,
        typer.Option(help="Write to this file [html default: the candidate's run directory]."),
    ] = None,
) -> None:
    """Statistical comparison of two runs: Δ with its CI, p- and q-values and a verdict."""
    project = load_project_or_exit(config)
    with config_errors():
        with open_store(project) as store:
            cand = resolve_run(store, candidate)
            base = resolve_baseline(store, baseline, cand)
        if base is None:
            raise typer.BadParameter(
                f"run {cand.id} has no earlier completed run of its suite to compare with",
                param_hint="BASELINE",
            )
        analyzed = analyze_runs(project, cand, base)
    if output_format == "terminal":
        render_terminal(analyzed.analysis, stdout)
        return
    report_format = ReportFormat(output_format)
    run_dir = runs_dir(project) / cand.id
    if out is None and report_format is not ReportFormat.HTML:
        texts = {
            ReportFormat.MD: render_markdown,
            ReportFormat.JSON: render_json,
            ReportFormat.JUNIT: render_junit,
        }
        # Machine-readable text goes to stdout as-is: no Rich markup or wrapping.
        typer.echo(texts[report_format](analyzed.analysis), nl=False)
        return
    target = out if out is not None else run_dir / FILE_NAMES[report_format]
    path = write_report(analyzed, report_format, target, run_dir=run_dir)
    stdout.print(f"Wrote {escape(str(path))}", soft_wrap=True)
