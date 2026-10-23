"""``hecklr run``: run a suite against the agent, store the results and summarize them (US-01).

The summary is ``hecklr-report``'s terminal report (every number with its interval), compared with
the recorded ``--baseline`` when there is one; the self-contained HTML report is written into the
run directory.
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Annotated

import anyio
import typer
from rich.markup import escape
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

from hecklr import exit_codes
from hecklr.commands._common import config_errors, fail, overrides, stderr, stdout
from hecklr.compose import (
    PreparedRun,
    RunRequest,
    check_model_providers,
    execute_run,
    prepare_run,
)
from hecklr.errors import describe_error
from hecklr.reports import FILE_NAMES, ReportFormat, write_report
from hecklr.results import analyze_results, load_results
from hecklr_core import BudgetEstimateError, RunResults
from hecklr_report import Analysis, fmt, render_terminal
from hecklr_report.analysis import nothing_got_through
from hecklr_schemas import Channel, Grade, LLMProvider, RunStatus, Trial

_STATUS_STYLE = {
    "ok": "green",
    "agent_error": "yellow",
    "timeout": "yellow",
    "harness_error": "red",
}


def run(
    ctx: typer.Context,
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to hecklr.yaml.")] = Path(
        "hecklr.yaml"
    ),
    suite: Annotated[
        str, typer.Option("--suite", "-s", help="Suite defined in hecklr.yaml.")
    ] = "ci",
    channels: Annotated[
        list[str] | None,
        typer.Option(help="Channels to run, e.g. `--channels chat` (repeat or comma-separate)."),
    ] = None,
    trials: Annotated[
        int | None, typer.Option(min=1, help="Trials per scenario x persona x channel.")
    ] = None,
    budget_usd: Annotated[
        float | None, typer.Option(min=0, help="Hard cost cap in USD (overrides budget.usd).")
    ] = None,
    seed: Annotated[int | None, typer.Option(help="Root seed (overrides seed).")] = None,
    adaptive: Annotated[
        bool | None,
        typer.Option(
            "--adaptive/--no-adaptive",
            help=(
                "Add up to 50% more trials where results are uncertain or flipped vs the "
                "baseline; each extra trial of a scenario runs once per persona x channel."
            ),
        ),
    ] = None,
    baseline: Annotated[
        str | None, typer.Option(help="Run id of the stored baseline to compare against later.")
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option(help="Where runs and the response cache go (overrides output_dir)."),
    ] = None,
) -> None:
    """Run a suite against the agent and write results."""
    request = RunRequest(
        config_path=config,
        suite=suite,
        channels=_parse_channels(channels),
        trials=trials,
        budget_usd=Decimal(str(budget_usd)) if budget_usd is not None else None,
        seed=seed,
        adaptive=adaptive,
        baseline_id=baseline,
        output_dir=output_dir,
    )
    prepared, results = run_with_progress(request, overrides(ctx).llm_providers)
    _summarize(prepared, results)
    raise typer.Exit(code=_exit_code(results))


def run_with_progress(
    request: RunRequest, llm_providers: Mapping[str, LLMProvider]
) -> tuple[PreparedRun, RunResults]:
    """Prepare, check and execute one run with live progress; CLI errors exit with their code.

    Exits 78 on a configuration error (including a model provider that cannot be set up, checked
    before the plan and its run id are shown, since no run is recorded then), 4 when the budget
    refuses the run, and 3 when the recorded run crashes.
    """
    with config_errors():
        try:
            prepared = prepare_run(request)
        except BudgetEstimateError as exc:
            raise fail(str(exc), exit_codes.BUDGET) from exc
        anyio.run(
            partial(
                check_model_providers,
                prepared.project.config.models,
                prepared.models_called,
                llm_providers or None,
            )
        )
    _print_plan(prepared)
    return prepared, _execute(prepared, llm_providers)


def _parse_channels(values: Sequence[str] | None) -> list[Channel] | None:
    if not values:
        return None
    names = [name.strip() for value in values for name in value.split(",") if name.strip()]
    try:
        return [Channel(name) for name in names]
    except ValueError as exc:
        known = ", ".join(channel.value for channel in Channel)
        raise typer.BadParameter(
            f"{exc}; known channels: {known}", param_hint="--channels"
        ) from exc


def _print_plan(prepared: PreparedRun) -> None:
    plan = prepared.plan
    stderr.print(
        f"Suite [bold]{escape(plan.suite.name)}[/]: {len(plan.run.matrix)} trials over "
        f"{len(plan.suite.cells)} scenarios, estimated cost at most ${plan.estimate.total_usd:.2f}"
        f"{' (adaptive)' if prepared.adaptive else ''}; run {plan.run.id}",
        soft_wrap=True,
    )


def _execute(prepared: PreparedRun, llm_providers: Mapping[str, LLMProvider]) -> RunResults:
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TextColumn("{task.fields[tally]}"),
        console=stderr,
        transient=True,
    )
    tally: dict[str, int] = {}
    task = progress.add_task("trials", total=len(prepared.plan.run.matrix), tally="")

    def on_trial(trial: Trial, grades: Sequence[Grade]) -> None:
        tally[trial.status] = tally.get(trial.status, 0) + 1
        text = " ".join(
            f"[{_STATUS_STYLE[status]}]{status} {count}[/]"
            for status, count in sorted(tally.items())
        )
        progress.update(task, advance=1, tally=text)

    def on_adaptive(extra: int) -> None:
        progress.update(task, total=(progress.tasks[0].total or 0) + extra)
        stderr.print(f"Adaptive pass: {extra} more trials")

    with progress:
        try:
            return anyio.run(
                partial(
                    execute_run,
                    prepared,
                    llm_providers=llm_providers or None,
                    on_trial=on_trial,
                    on_adaptive=on_adaptive,
                )
            )
        except Exception as exc:
            # The executor already recorded the run as failed; say why and stop.
            raise fail(f"the run failed: {describe_error(exc)}", exit_codes.HARNESS_ERROR) from exc


def _summarize(prepared: PreparedRun, results: RunResults) -> None:
    """Print the terminal report and write the HTML one next to the results."""
    project = prepared.project
    run = results.run
    baseline = load_results(project, run.baseline_id) if run.baseline_id is not None else None
    analyzed = analyze_results(project, results, baseline)
    render_terminal(analyzed.analysis, stdout)
    stdout.print(scenario_table(analyzed.analysis))
    run_dir = prepared.runs_dir / run.id
    report = write_report(
        analyzed, ReportFormat.HTML, run_dir / FILE_NAMES[ReportFormat.HTML], run_dir=run_dir
    )
    # Never wrapped, so the run id and paths stay copyable on narrow terminals.
    stdout.print(f"Spent: ${run.spent_usd:.4f} of budget {_budget(results)}", soft_wrap=True)
    stdout.print(f"Run [bold]{run.id}[/] ({run.status.value})", soft_wrap=True)
    stdout.print(f"Results: {escape(str(run_dir))}", soft_wrap=True)
    stdout.print(f"Report: {escape(str(report))}", soft_wrap=True)


def scenario_table(analysis: Analysis) -> Table:
    """Every scenario's pass rate with its Wilson interval (US-01).

    ``render_terminal`` lists only the scenarios that failed or flipped; a run's own summary
    shows all of them.
    """
    table = Table(title="Scenarios (Wilson 95% CI)", title_justify="left")
    for name in ("scenario", "passed", "pass rate [95% CI]"):
        table.add_column(name, justify="left" if name == "scenario" else "right")
    for result in analysis.scenarios:
        rate = result.pass_rate
        table.add_row(
            Text(result.scenario_id),
            f"{result.passes}/{result.trials}",
            fmt.estimate(rate, "percent") if rate is not None else "not analyzable",
        )
    return table


def _budget(results: RunResults) -> str:
    usd = results.run.budget.usd
    return f"${usd:.2f}" if usd is not None else "unlimited"


def _exit_code(results: RunResults) -> int:
    run = results.run
    if run.status is RunStatus.BUDGET_EXCEEDED:
        stderr.print("[bold red]The run stopped when spend reached the budget.[/]")
        return exit_codes.BUDGET
    if run.status is not RunStatus.COMPLETED:
        return exit_codes.HARNESS_ERROR
    if results.trials and all(trial.status == "harness_error" for trial in results.trials):
        stderr.print(
            "[bold red]Every trial failed to run.[/] First error: "
            f"{escape(results.trials[0].error or 'unknown')}",
            soft_wrap=True,
        )
        return exit_codes.HARNESS_ERROR
    if (blocked := nothing_got_through(results)) is not None:
        # A setup that never let one conversation through measured nothing (ADR 00hra).
        stderr.print(f"[bold red]No trial got through:[/] {escape(blocked)}", soft_wrap=True)
        return exit_codes.HARNESS_ERROR
    return exit_codes.OK
