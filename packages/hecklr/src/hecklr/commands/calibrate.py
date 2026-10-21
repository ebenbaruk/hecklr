"""``hecklr calibrate``: self-tests that show Hecklr's verdicts can be trusted.

- ``--aa`` (SC5): the same agent run twice must not be a regression. Runs the suite twice with
  different seeds (the same seed would replay the response cache and prove nothing), or compares
  two stored runs (``--runs A --runs B``), then simulates 200 A/A comparisons of a suite of the
  same shape to measure the false-alarm rate (and the power to see a 10-point drop).
- ``--judges`` (SC7): agreement of every judge rubric with human labels (Cohen's κ, precision
  and recall, with intervals); rubrics below κ 0.70 are untrusted.
- ``--voice-loopback`` (SC4) is not wired into the CLI yet.

Exit codes: 0 every check passed, 1 a check failed (A/A flagged a regression or a false-alarm
rate above alpha; an untrusted judge), 3 a run could not be analyzed, 69 not available, 78 config.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Final

import typer
from pydantic import ValidationError
from rich.table import Table
from rich.text import Text

from hecklr import exit_codes
from hecklr.commands._common import (
    DEFAULT_CONFIG,
    ConfigPath,
    config_errors,
    fail,
    load_project_or_exit,
    overrides,
    stdout,
)
from hecklr.commands.run import run_with_progress
from hecklr.compose import RunRequest
from hecklr.results import LATEST, analyze_results, load_results, resolve_run, runs_dir
from hecklr_core import Project, ResultStore, RunResults
from hecklr_report import fmt, render_terminal
from hecklr_schemas import Grade, HumanLabel, Verdict
from hecklr_stats import JudgeCalibration, calibrate_judge, verdict_rate

SIMULATED_COMPARISONS: Final = 200
"""A/A comparisons simulated at the suite's shape, as SC5 measures (200)."""
POWER_DROP: Final = 0.10
"""The pass-rate drop whose detection power is reported (SC6: 10 points)."""
JUDGE_PREFIX: Final = "judge:"


def calibrate(
    ctx: typer.Context,
    config: ConfigPath = DEFAULT_CONFIG,
    aa: Annotated[
        bool, typer.Option("--aa", help="A/A self-test: false-alarm rate (SC5).")
    ] = False,
    judges: Annotated[
        bool, typer.Option("--judges", help="Judge agreement with human labels (SC7).")
    ] = False,
    voice_loopback: Annotated[
        bool, typer.Option("--voice-loopback", help="Voice timing loopback (SC4).")
    ] = False,
    suite: Annotated[str, typer.Option("--suite", "-s", help="Suite for --aa runs.")] = "ci",
    trials: Annotated[
        int | None, typer.Option(min=1, help="Trials per cell for --aa runs.")
    ] = None,
    seed: Annotated[
        int | None, typer.Option(help="Root seed of the first --aa run; the second uses seed+1.")
    ] = None,
    runs: Annotated[
        list[str] | None,
        typer.Option(help="--aa: compare these two stored runs instead of running (give twice)."),
    ] = None,
    replicates: Annotated[
        int,
        typer.Option(min=0, help="--aa: simulated A/A comparisons (0 skips the simulation)."),
    ] = SIMULATED_COMPARISONS,
    labels: Annotated[
        Path | None,
        typer.Option(exists=True, dir_okay=False, help="--judges: human labels, JSONL."),
    ] = None,
    judged_runs: Annotated[
        list[str] | None,
        typer.Option("--run", help="--judges: runs whose judge grades were labeled [latest]."),
    ] = None,
) -> None:
    """Self-tests: A/A false-positive rate, judge agreement, voice loopback."""
    if voice_loopback:
        raise fail(
            "--voice-loopback is not available in the CLI yet; the loopback calibration runs in "
            "hecklr-voice's test suite (SC4)",
            exit_codes.NOT_IMPLEMENTED,
        )
    if not (aa or judges):
        raise fail("choose a self-test: --aa or --judges", exit_codes.CONFIG_ERROR)
    project = load_project_or_exit(config)
    passed = True
    if aa:
        pair = _aa_runs(ctx, project, config, suite, trials, seed, runs)
        passed &= _report_aa(project, *pair, replicates=replicates)
    if judges:
        if labels is None:
            raise fail(
                "--judges needs --labels FILE (JSONL of human labels)", exit_codes.CONFIG_ERROR
            )
        passed &= _report_judges(project, labels, judged_runs or [LATEST])
    raise typer.Exit(code=exit_codes.OK if passed else exit_codes.CHECK_FAILED)


# --- A/A ----------------------------------------------------------------------------------------


def _aa_runs(
    ctx: typer.Context,
    project: Project,
    config: Path,
    suite: str,
    trials: int | None,
    seed: int | None,
    runs: Sequence[str] | None,
) -> tuple[RunResults, RunResults]:
    if runs:
        if len(runs) != 2:
            raise fail("--runs takes exactly two runs (give it twice)", exit_codes.CONFIG_ERROR)
        with config_errors():
            store = ResultStore(runs_dir(project))
            first, second = (load_results(project, resolve_run(store, ref).id) for ref in runs)
            return first, second
    root_seed = seed if seed is not None else project.config.seed
    request = RunRequest(config_path=config, suite=suite, trials=trials, seed=root_seed)
    providers = overrides(ctx).llm_providers
    stdout.print("A/A: running the same agent twice, with different seeds.")
    _, first = run_with_progress(request, providers)
    _, second = run_with_progress(replace(request, seed=root_seed + 1), providers)
    return first, second


def _report_aa(project: Project, first: RunResults, second: RunResults, *, replicates: int) -> bool:
    analysis = analyze_results(project, second, first).analysis
    render_terminal(analysis, stdout)
    if analysis.problems:
        raise fail(
            "the A/A runs cannot be compared: " + "; ".join(analysis.problems),
            exit_codes.HARNESS_ERROR,
        )
    flagged = analysis.verdict is Verdict.REGRESSION
    stdout.print(
        Text.assemble(
            "A/A comparison: ",
            ("FAILED, a regression was flagged", "bold red")
            if flagged
            else ("passed, no regression flagged", "bold green"),
        )
    )
    if replicates == 0:
        return not flagged
    stats = project.config.stats
    n_scenarios = len(analysis.scenarios)
    trials = max(1, round(analysis.counts.analyzed / max(n_scenarios, 1)))
    seed = analysis.seed
    false_alarms = verdict_rate(
        n_scenarios=n_scenarios,
        trials=trials,
        drop=0.0,
        replicates=replicates,
        config=stats,
        seed=seed,
    )
    power = verdict_rate(
        n_scenarios=n_scenarios,
        trials=trials,
        drop=POWER_DROP,
        replicates=replicates,
        config=stats,
        seed=seed + 1,
    )
    too_many = false_alarms.value > stats.alpha
    shape = f"{n_scenarios} scenarios x {trials} trials, {replicates} simulated comparisons"
    stdout.print(
        f"Simulated A/A false-alarm rate ({shape}): "
        f"{fmt.estimate(false_alarms, 'percent')}, target <= {stats.alpha:.0%}"
        + (" [bold red]FAILED[/]" if too_many else " [green]ok[/]"),
        soft_wrap=True,
    )
    stdout.print(
        f"Power to detect a {100 * POWER_DROP:.0f}-point pass-rate drop at this size: "
        f"{fmt.estimate(power, 'percent')} (SC6 asks >= 80% on the reference suite)",
        soft_wrap=True,
    )
    return not (flagged or too_many)


# --- judges -------------------------------------------------------------------------------------


def _report_judges(project: Project, labels_path: Path, refs: Sequence[str]) -> bool:
    labels = _read_labels(labels_path)
    with config_errors():
        store = ResultStore(runs_dir(project))
        grades = [
            grade
            for ref in refs
            for grade in load_results(project, resolve_run(store, ref).id).grades
            if grade.grader.startswith(JUDGE_PREFIX) and grade.status == "ok"
        ]
    stats = project.config.stats
    seed = stats.seed if stats.seed is not None else project.config.seed
    calibrations = [
        calibrate_judge(group, labels, seed=seed, resamples=stats.bootstrap_resamples)
        for group in _judge_groups(grades, labels)
    ]
    if not calibrations:
        raise fail(
            f"no label in {labels_path} matches a judge grade of the runs {', '.join(refs)}",
            exit_codes.CONFIG_ERROR,
        )
    stdout.print(_judge_table(calibrations))
    return all(c.trusted for c in calibrations)


def _read_labels(path: Path) -> list[HumanLabel]:
    labels = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            labels.append(HumanLabel.model_validate_json(line))
        except ValidationError as exc:
            raise fail(
                f"{path}:{number}: not a human label: {exc.errors()[0]['msg']}",
                exit_codes.CONFIG_ERROR,
            ) from exc
    return labels


def _judge_groups(grades: Sequence[Grade], labels: Sequence[HumanLabel]) -> list[list[Grade]]:
    """Grades per (rubric, version, judge model), for the rubric versions humans labeled."""
    labeled = {(label.grader, label.grader_version) for label in labels}
    groups: defaultdict[tuple[str, str, str | None], list[Grade]] = defaultdict(list)
    for grade in grades:
        if (grade.grader, grade.grader_version) in labeled:
            groups[(grade.grader, grade.grader_version, grade.judge_model)].append(grade)
    return [groups[key] for key in sorted(groups, key=lambda k: (k[0], k[1], k[2] or ""))]


def _judge_table(calibrations: Sequence[JudgeCalibration]) -> Table:
    table = Table(title="Judge agreement with human labels (95% CI)", title_justify="left")
    for name in ("rubric", "version", "model", "n", "kappa", "precision", "recall", "trusted"):
        table.add_column(name)
    for c in calibrations:
        table.add_row(
            Text(c.grader),
            Text(c.grader_version),
            Text(c.judge_model or "-"),
            str(c.n),
            fmt.estimate(c.kappa, "number"),
            fmt.estimate(c.precision, "percent") if c.precision is not None else "-",
            fmt.estimate(c.recall, "percent") if c.recall is not None else "-",
            Text("yes", "green") if c.trusted else Text("NO (kappa < 0.70)", "bold red"),
        )
    return table
