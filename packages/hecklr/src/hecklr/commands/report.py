"""``hecklr report [RUN]``: write a stored run's report files and print where they are."""

import webbrowser
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from hecklr import exit_codes
from hecklr.commands._common import (
    DEFAULT_CONFIG,
    ConfigPath,
    config_errors,
    fail,
    load_project_or_exit,
    stdout,
)
from hecklr.reports import FILE_NAMES, ReportFormat, write_report
from hecklr.results import (
    LATEST,
    analyze_runs,
    open_store,
    resolve_baseline,
    resolve_run,
    runs_dir,
)


def report(
    run: Annotated[str, typer.Argument(help="Run id or `latest`.")] = LATEST,
    config: ConfigPath = DEFAULT_CONFIG,
    formats: Annotated[
        list[ReportFormat] | None,
        typer.Option("--format", "-f", help="Formats to write (repeatable) [default: html]."),
    ] = None,
    baseline: Annotated[
        str | None,
        typer.Option(
            help="Compare with this run (an id or `baseline`) [default: the run's recorded one]."
        ),
    ] = None,
    out_dir: Annotated[
        Path | None, typer.Option(help="Directory for the files [default: the run directory].")
    ] = None,
    open_report: Annotated[
        bool, typer.Option("--open", help="Open the HTML report in a browser.")
    ] = False,
) -> None:
    """Build HTML, Markdown, JSON or JUnit output for a run."""
    project = load_project_or_exit(config)
    chosen = list(dict.fromkeys(formats or [ReportFormat.HTML]))
    if open_report and ReportFormat.HTML not in chosen:
        raise fail("--open needs the html format", exit_codes.CONFIG_ERROR)
    with config_errors():
        with open_store(project) as store:
            candidate = resolve_run(store, run)
            reference = baseline or candidate.baseline_id
            base = resolve_baseline(store, reference, candidate) if reference is not None else None
        analyzed = analyze_runs(project, candidate, base)
    run_dir = runs_dir(project) / candidate.id
    target_dir = out_dir if out_dir is not None else run_dir
    written = {
        report_format: write_report(
            analyzed, report_format, target_dir / FILE_NAMES[report_format], run_dir=run_dir
        )
        for report_format in chosen
    }
    for path in written.values():
        stdout.print(escape(str(path)), soft_wrap=True)
    if open_report:
        webbrowser.open(written[ReportFormat.HTML].resolve().as_uri())
