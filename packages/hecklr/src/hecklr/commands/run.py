"""``hecklr run``: run a suite against the agent, store the results and summarize them (US-01)."""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Annotated

import anyio
import typer
from rich.markup import escape
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

from hecklr import exit_codes
from hecklr.commands._common import CONFIG_ERRORS, config_errors, fail, overrides, stderr, stdout
from hecklr.compose import PreparedRun, RunRequest, execute_run, prepare_run
from hecklr.errors import describe_error
from hecklr.summary import render_summary, summarize
from hecklr_core import BudgetEstimateError, RunResults
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
    with config_errors():
        try:
            prepared = prepare_run(request)
        except BudgetEstimateError as exc:
            raise fail(str(exc), exit_codes.BUDGET) from exc
    _print_plan(prepared)
    results = _execute(prepared, overrides(ctx).llm_providers)
    config_model = prepared.project.config
    summary = summarize(
        results,
        results_dir=prepared.runs_dir / results.run.id,
        resamples=config_model.stats.bootstrap_resamples,
        seed=config_model.stats.seed if config_model.stats.seed is not None else results.run.seed,
    )
    render_summary(summary, stdout)
    raise typer.Exit(code=_exit_code(results))


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
        except CONFIG_ERRORS as exc:
            # A model provider that cannot be set up (e.g. a missing API key) is found before the
            # run is recorded: a configuration problem, not a harness failure.
            raise fail(str(exc), exit_codes.CONFIG_ERROR) from exc
        except Exception as exc:
            # The executor already recorded the run as failed; say why and stop.
            raise fail(f"the run failed: {describe_error(exc)}", exit_codes.HARNESS_ERROR) from exc


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
    return exit_codes.OK
