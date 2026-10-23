"""``hecklr bench``: run, reproduce and publish (locally) the benchmark of ``bench/`` (US-12).

- ``hecklr bench list``: the stacks, their track, what they need and what one run may cost;
- ``hecklr bench run --stack ID`` (or ``--all``): measure stacks and write ``results/<id>.json``;
- ``hecklr bench reproduce --stack ID``: measure a stack again and check every metric against
  its published row (exit 1 when an interval no longer overlaps);
- ``hecklr bench site``: build the static leaderboard into ``bench/site/dist``.
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Final

import typer
from rich.markup import escape
from rich.table import Table

from hecklr import exit_codes
from hecklr.commands._common import CONFIG_ERRORS, config_errors, fail, overrides, stderr, stdout
from hecklr.commands.bench.process import StackError
from hecklr.commands.bench.result import (
    MetricCheck,
    StackResult,
    check_reproduction,
    missing_metrics,
)
from hecklr.commands.bench.runner import BenchPaths, run_stack, suite_digest
from hecklr.commands.bench.site import build_site, load_results
from hecklr.commands.bench.spec import (
    RESULTS_DIR,
    Manifest,
    StackSpec,
    load_manifest,
    load_stacks,
    select_stacks,
)
from hecklr.errors import describe_error
from hecklr_core import BudgetEstimateError
from hecklr_report import fmt
from hecklr_schemas import Estimate, LLMProvider, Trial

app = typer.Typer(
    name="bench",
    help="Run or reproduce the public benchmark and build its leaderboard.",
    no_args_is_help=True,
)

BenchDir = Annotated[Path, typer.Option(help="The benchmark directory (holds bench.yaml).")]
WorkDir = Annotated[
    Path, typer.Option(help="Where project copies, result stores and agent logs go.")
]
DEFAULT_BENCH: Final = Path("bench")
DEFAULT_WORK: Final = Path(".hecklr/bench")


@app.command("list")
def list_stacks(bench_dir: BenchDir = DEFAULT_BENCH) -> None:
    """The benchmark's stacks."""
    with config_errors():
        manifest = load_manifest(bench_dir)
        stacks = load_stacks(bench_dir)
    published = {row.stack.id for row in _published(bench_dir)}
    table = Table(title=f"Hecklr benchmark {manifest.version}", title_justify="left")
    for column in ("stack", "track", "needs", "max $ per run", "published"):
        table.add_column(column)
    for stack in stacks.values():
        table.add_row(
            stack.id,
            stack.track,
            ", ".join(stack.requires_env) or "nothing",
            f"{stack.max_usd:.2f}",
            "yes" if stack.id in published else "no",
        )
    stdout.print(table)


@app.command()
def run(
    ctx: typer.Context,
    stack: Annotated[
        list[str] | None, typer.Option("--stack", "-s", help="Stack id (repeatable).")
    ] = None,
    all_stacks: Annotated[bool, typer.Option("--all", help="Every stack.")] = False,
    budget_usd: Annotated[
        float | None,
        typer.Option(min=0, help="Refuse to start when the stacks' caps add up to more."),
    ] = None,
    bench_dir: BenchDir = DEFAULT_BENCH,
    work_dir: WorkDir = DEFAULT_WORK,
) -> None:
    """Measure stacks and write their leaderboard rows to ``results/<stack>.json``."""
    manifest, chosen = _choose(bench_dir, stack, all_stacks)
    _check_budget(chosen, budget_usd)
    paths = BenchPaths(bench_dir, work_dir)
    providers = overrides(ctx).llm_providers
    failed = []
    for spec in chosen:
        try:
            result = _measure(manifest, spec, paths, providers)
        except StackError as exc:
            stderr.print(f"[bold red]{spec.id} not measured:[/] {escape(str(exc))}", soft_wrap=True)
            failed.append(spec.id)
            continue
        path = bench_dir / RESULTS_DIR / f"{spec.id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        stdout.print(row_table(result))
        stdout.print(f"Wrote {escape(str(path))}", soft_wrap=True)
    if failed:
        raise typer.Exit(code=exit_codes.HARNESS_ERROR)


@app.command()
def reproduce(
    ctx: typer.Context,
    stack: Annotated[str, typer.Option("--stack", "-s", help="Stack id of a published row.")],
    bench_dir: BenchDir = DEFAULT_BENCH,
    work_dir: WorkDir = DEFAULT_WORK,
) -> None:
    """Measure a stack again and check its metrics against the published row (US-12)."""
    manifest, (spec,) = _choose(bench_dir, [stack], False)
    path = bench_dir / RESULTS_DIR / f"{spec.id}.json"
    if not path.is_file():
        raise fail(
            f"no published row {path}; run `hecklr bench run --stack {spec.id}`",
            exit_codes.CONFIG_ERROR,
        )
    published = StackResult.model_validate_json(path.read_bytes())
    _check_same_benchmark(published, manifest, bench_dir)
    try:
        reproduced = _measure(
            manifest, spec, BenchPaths(bench_dir, work_dir), overrides(ctx).llm_providers
        )
    except StackError as exc:
        raise fail(f"{spec.id} not measured: {exc}", exit_codes.HARNESS_ERROR) from exc
    out = work_dir / f"{spec.id}.reproduction.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(reproduced.model_dump_json(indent=2) + "\n", encoding="utf-8")
    checks = check_reproduction(published, reproduced)
    stdout.print(checks_table(spec.id, checks))
    missing = missing_metrics(published, reproduced)
    if missing:
        stderr.print(f"[bold red]Not measured again:[/] {', '.join(missing)}")
    inconsistent = [c.metric for c in checks if not c.consistent]
    stdout.print(f"Reproduction: {escape(str(out))}", soft_wrap=True)
    if inconsistent or missing:
        stdout.print(f"[bold red]NOT REPRODUCED[/]: {', '.join(inconsistent + list(missing))}")
        raise typer.Exit(code=exit_codes.CHECK_FAILED)
    stdout.print(f"[bold green]REPRODUCED[/]: all {len(checks)} metrics consistent")


@app.command()
def site(
    bench_dir: BenchDir = DEFAULT_BENCH,
    out: Annotated[
        Path | None, typer.Option(help="Output directory (default: <bench-dir>/site/dist).")
    ] = None,
) -> None:
    """Build the static leaderboard from the published rows (built locally, never deployed)."""
    with config_errors():
        manifest = load_manifest(bench_dir)
        rows = load_results(bench_dir / RESULTS_DIR)
    index = build_site(
        manifest, rows, source=bench_dir / "site" / "src", out=out or bench_dir / "site" / "dist"
    )
    stdout.print(f"{len(rows)} rows; open {escape(str(index.resolve()))}", soft_wrap=True)


def _choose(
    bench_dir: Path, ids: Sequence[str] | None, all_stacks: bool
) -> tuple[Manifest, list[StackSpec]]:
    with config_errors():
        manifest = load_manifest(bench_dir)
        stacks = load_stacks(bench_dir)
        if all_stacks:
            return manifest, list(stacks.values())
        if not ids:
            raise fail("name a stack with --stack, or pass --all", exit_codes.CONFIG_ERROR)
        return manifest, select_stacks(stacks, list(ids))


def _check_budget(stacks: Sequence[StackSpec], budget_usd: float | None) -> None:
    caps = sum((s.max_usd for s in stacks), Decimal(0))
    if budget_usd is not None and caps > Decimal(str(budget_usd)):
        raise fail(
            f"the stacks may spend up to ${caps:.2f}, more than --budget-usd {budget_usd:.2f}",
            exit_codes.BUDGET,
        )


def _check_same_benchmark(published: StackResult, manifest: Manifest, bench_dir: Path) -> None:
    if published.benchmark_version != manifest.version:
        raise fail(
            f"the row was measured on benchmark {published.benchmark_version}, this checkout is "
            f"{manifest.version}",
            exit_codes.CONFIG_ERROR,
        )
    if published.suite_digest != suite_digest(bench_dir, manifest):
        raise fail(
            "the suite changed since the row was measured (suite digest differs); "
            "bump bench.yaml's version and re-run the stack",
            exit_codes.CONFIG_ERROR,
        )


def _measure(
    manifest: Manifest,
    spec: StackSpec,
    paths: BenchPaths,
    providers: Mapping[str, LLMProvider],
) -> StackResult:
    stderr.print(f"[bold]{spec.id}[/]: {escape(spec.name)} ({spec.track})", soft_wrap=True)

    def report(trial: Trial, agent_usd: Decimal) -> None:
        seconds = trial.duration_ms / 1000
        cost = trial.cost_usd + agent_usd
        stderr.print(
            f"  {trial.scenario_id} #{trial.index}: {trial.status}, {seconds:.1f} s, ${cost:.4f}",
            soft_wrap=True,
        )

    with config_errors():
        try:
            return run_stack(
                manifest, spec, paths, llm_providers=providers or None, on_trial=report
            )
        except BudgetEstimateError as exc:
            raise fail(str(exc), exit_codes.BUDGET) from exc
        except (StackError, *CONFIG_ERRORS):
            raise
        except Exception as exc:
            # Anything else crashed this stack's run; `run --all` goes on with the next stack.
            raise StackError(describe_error(exc)) from exc


def _published(bench_dir: Path) -> list[StackResult]:
    with config_errors():
        return load_results(bench_dir / RESULTS_DIR)


def row_table(result: StackResult) -> Table:
    """One stack's metrics, each with its interval."""
    table = Table(
        title=f"{result.stack.id}: {result.counts.analyzed} of {result.counts.total} "
        f"conversations analyzed, spent ${result.spend.total_usd:.4f}",
        title_justify="left",
    )
    table.add_column("metric")
    table.add_column("value [95% CI]", justify="right")
    for name, estimate in result.metrics.items():
        table.add_row(name, format_estimate(name, estimate))
    return table


def checks_table(stack_id: str, checks: Sequence[MetricCheck]) -> Table:
    """Published vs reproduced, per metric."""
    table = Table(title=f"{stack_id}: published vs reproduced", title_justify="left")
    for column in ("metric", "published", "reproduced", "in published CI", "consistent"):
        table.add_column(column)
    for check in checks:
        table.add_row(
            check.metric,
            format_estimate(check.metric, check.published),
            format_estimate(check.metric, check.reproduced),
            "yes" if check.within else "no",
            "[green]yes[/]" if check.consistent else "[red]no[/]",
        )
    return table


def format_estimate(metric: str, estimate: Estimate) -> str:
    """A metric's value with its interval, in its unit."""
    if metric.endswith("_usd"):
        return f"${estimate.value:.4f} [{estimate.ci_low:.4f}, {estimate.ci_high:.4f}]"
    if metric == "task_success" or metric.startswith("judge:"):
        return fmt.estimate(estimate, "percent")
    if "latency" in metric or "reaction" in metric:
        return f"{estimate.value:,.0f} ms [{estimate.ci_low:,.0f}, {estimate.ci_high:,.0f}]"
    return f"{estimate.value:.2f} [{estimate.ci_low:.2f}, {estimate.ci_high:.2f}]"
