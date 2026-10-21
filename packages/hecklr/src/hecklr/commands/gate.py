"""``hecklr gate``: compare a run with its baseline and turn the verdict into a CI exit code.

Exit codes (spec, "CLI"): 0 pass, 1 regression, 2 inconclusive (0 with
``gate.on_inconclusive: pass``), 3 harness error. Any problem finding or reading the runs is a
harness error too, so CI never mistakes a broken gate for a verdict. The Markdown PR comment goes
to stdout; the comment, HTML, JSON and JUnit reports are written to ``--out-dir``.
"""

from pathlib import Path
from typing import Annotated, Final, Literal

import typer
from rich.markup import escape

from hecklr.commands._common import (
    CONFIG_ERRORS,
    DEFAULT_CONFIG,
    ConfigPath,
    config_errors,
    fail,
    load_project_or_exit,
    stderr,
)
from hecklr.errors import describe_error
from hecklr.reports import FILE_NAMES, ReportFormat, write_report
from hecklr.results import (
    BASELINE,
    LATEST,
    Analyzed,
    analyze_runs,
    open_store,
    resolve_baseline,
    resolve_run,
    runs_dir,
)
from hecklr_core import RunNotFoundError
from hecklr_report import EXIT_HARNESS_ERROR, EXIT_PASS, gate_exit_code
from hecklr_schemas import GateConfig

OUTCOMES: Final = {0: "PASS", 1: "REGRESSION", 2: "INCONCLUSIVE", 3: "HARNESS ERROR"}

MissingBaseline = Literal["error", "pass"]
"""What a gate with no baseline run at all does (a repository's very first gated run)."""


def gate(
    config: ConfigPath = DEFAULT_CONFIG,
    suite: Annotated[
        str | None, typer.Option("--suite", "-s", help="Gate the latest run of this suite.")
    ] = None,
    candidate: Annotated[str, typer.Option(help="Candidate run: an id or `latest`.")] = LATEST,
    baseline: Annotated[
        str,
        typer.Option(
            help="Baseline run: an id, or `baseline` (the candidate's recorded baseline, else the "
            "newest earlier completed run of its suite)."
        ),
    ] = BASELINE,
    on_inconclusive: Annotated[
        Literal["fail", "pass"] | None,
        typer.Option(help="Exit code of an inconclusive verdict [default: gate.on_inconclusive]."),
    ] = None,
    missing_baseline: Annotated[
        MissingBaseline,
        typer.Option(
            help="With no baseline run at all: `error` exits 3; `pass` reports the candidate "
            "alone and exits 0 when the candidate itself is sound."
        ),
    ] = "error",
    out_dir: Annotated[
        Path | None,
        typer.Option(help="Where the reports go [default: the candidate's run directory]."),
    ] = None,
    report_url: Annotated[
        str | None, typer.Option(help="Link to the full report, shown in the comment.")
    ] = None,
) -> None:
    """Compare against the stored baseline and set the CI exit code."""
    project = load_project_or_exit(config, EXIT_HARNESS_ERROR)
    gate_config = project.config.gate
    if on_inconclusive is not None:
        gate_config = gate_config.model_copy(update={"on_inconclusive": on_inconclusive})
    with config_errors(EXIT_HARNESS_ERROR):
        with open_store(project) as store:
            cand = resolve_run(store, candidate, suite=suite)
            base = resolve_baseline(store, baseline, cand)
        run_dir = runs_dir(project) / cand.id
        target = out_dir if out_dir is not None else run_dir
        try:
            analyzed = analyze_runs(project, cand, base)
            _write_reports(analyzed, target, run_dir, report_url)
        except (*CONFIG_ERRORS, RunNotFoundError):
            raise
        except Exception as exc:
            # CI reads any other exit as a verdict (a traceback exits 1, "regression"), so a gate
            # that broke while analyzing or writing must say so with the harness-error code.
            raise fail(f"the gate failed: {describe_error(exc)}", EXIT_HARNESS_ERROR) from exc
    typer.echo((target / FILE_NAMES[ReportFormat.MD]).read_text(encoding="utf-8"), nl=False)
    code = decide(analyzed, gate_config, missing_baseline)
    stderr.print(
        f"Gate: [bold]{OUTCOMES[code]}[/] (exit {code}); reports in {escape(str(target))}",
        soft_wrap=True,
    )
    raise typer.Exit(code=code)


def _write_reports(analyzed: Analyzed, target: Path, run_dir: Path, report_url: str | None) -> None:
    for report_format in ReportFormat:
        write_report(
            analyzed,
            report_format,
            target / FILE_NAMES[report_format],
            run_dir=run_dir,
            report_url=report_url,
        )


def decide(analyzed: Analyzed, gate_config: GateConfig, missing_baseline: MissingBaseline) -> int:
    """The gate's exit code, including what a missing baseline means (ADR 0007c)."""
    analysis = analyzed.analysis
    if analyzed.baseline is None:
        if missing_baseline == "pass" and not analysis.problems:
            stderr.print(
                f"No baseline run for {analysis.candidate.id}: nothing to compare with, "
                "so the gate passes."
            )
            return EXIT_PASS
        stderr.print(f"[bold red]No baseline run[/] to compare {analysis.candidate.id} with.")
    return gate_exit_code(analysis, gate_config)
