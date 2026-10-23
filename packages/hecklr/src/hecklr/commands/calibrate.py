"""``hecklr calibrate``: self-tests that show Hecklr's verdicts can be trusted.

- ``--aa`` (SC5): the same agent run twice must not be a regression. Runs the suite twice with
  different seeds (the same seed would replay the response cache and prove nothing), or compares
  two stored runs (``--runs A --runs B``), then simulates 200 A/A comparisons of a suite of the
  same shape to measure the false-alarm rate (and the power to see a 10-point drop).
- ``--judges`` (SC7): agreement of every judge rubric with human labels (Cohen's κ, precision
  and recall, with intervals); rubrics below κ 0.70 are untrusted. The labels are either of judge
  grades in stored runs (``--labels FILE --run ID``, written by the UI's labeling queue) or of a
  labeled transcript set (``--set DIR``, e.g. ``calibration/judges``), which is judged on the
  spot with the project's judge model (ADR 00hja). ``--labeler`` keeps only some labelers' labels.
- ``--voice-loopback`` (SC4): calls an in-process loopback agent with known timing through the
  real voice driver; response latency and barge-in reaction must be within 20 ms of the truth.

Exit codes: 0 every check passed, 1 a check failed (A/A flagged a regression or a false-alarm
rate above alpha; an untrusted judge; a voice timing error above 20 ms), 3 a run could not be
analyzed, 78 config.
"""

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Final

import anyio
import typer
from rich.table import Table
from rich.text import Text

from hecklr import exit_codes, judge_calibration, voice_calibration
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
from hecklr.compose import RunRequest, check_model_providers
from hecklr.judge_calibration import CalibrationSet, CalibrationSetError
from hecklr.results import LATEST, analyze_results, load_results, open_store, resolve_run
from hecklr.testing import scripted_models
from hecklr_core import Project, RunResults, open_llm
from hecklr_report import fmt, render_terminal
from hecklr_schemas import Estimate, Grade, HumanLabel, LLMProvider, Verdict
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
    calibration_set: Annotated[
        Path | None,
        typer.Option(
            "--set",
            exists=True,
            file_okay=False,
            help="--judges: a labeled transcript set to judge now (e.g. calibration/judges).",
        ),
    ] = None,
    labelers: Annotated[
        list[str] | None,
        typer.Option(
            "--labeler",
            help="--judges: only labels whose labeler starts with this, e.g. human: (repeatable).",
        ),
    ] = None,
) -> None:
    """Self-tests: A/A false-positive rate, judge agreement, voice loopback."""
    if not (aa or judges or voice_loopback):
        raise fail(
            "choose a self-test: --aa, --judges or --voice-loopback", exit_codes.CONFIG_ERROR
        )
    if calibration_set is not None and judged_runs:
        raise fail("--set judges its own transcripts; drop --run", exit_codes.CONFIG_ERROR)
    passed = True
    if voice_loopback:
        passed &= _report_voice_loopback(seed or 0)
    if not (aa or judges):
        raise typer.Exit(code=exit_codes.OK if passed else exit_codes.CHECK_FAILED)
    project = load_project_or_exit(config)
    if aa:
        pair = _aa_runs(ctx, project, config, suite, trials, seed, runs)
        passed &= _report_aa(project, *pair, replicates=replicates)
    if judges and calibration_set is not None:
        providers = overrides(ctx).llm_providers or scripted_models()
        passed &= _report_judge_set(project, calibration_set, labels, labelers or [], providers)
    elif judges:
        if labels is None:
            raise fail(
                "--judges needs --labels FILE (JSONL of human labels) or --set DIR",
                exit_codes.CONFIG_ERROR,
            )
        passed &= _report_judges(project, labels, judged_runs or [LATEST], labelers or [])
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
        return _stored_pair(project, runs)
    root_seed = seed if seed is not None else project.config.seed
    request = RunRequest(config_path=config, suite=suite, trials=trials, seed=root_seed)
    providers = overrides(ctx).llm_providers
    stdout.print("A/A: running the same agent twice, with different seeds.")
    _, first = run_with_progress(request, providers)
    _, second = run_with_progress(replace(request, seed=root_seed + 1), providers)
    return first, second


def _stored_pair(project: Project, refs: Sequence[str]) -> tuple[RunResults, RunResults]:
    if len(refs) != 2:
        raise fail("--runs takes exactly two runs (give it twice)", exit_codes.CONFIG_ERROR)
    with config_errors(), open_store(project) as store:
        first, second = (resolve_run(store, ref).id for ref in refs)
    if first == second:
        # The same run twice always agrees with itself and would prove nothing.
        raise fail(f"--runs names the same run {first} twice", exit_codes.CONFIG_ERROR)
    with config_errors():
        return load_results(project, first), load_results(project, second)


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


# --- voice loopback -------------------------------------------------------------------------------


def _report_voice_loopback(seed: int) -> bool:
    stdout.print(
        "Voice loopback (SC4): calling a loopback agent with known timing through the voice "
        "driver, in real time (about 30 s)."
    )
    with config_errors():
        checks = anyio.run(voice_calibration.run_voice_loopback, seed)
    table = Table(
        title="Voice timing error vs the injected truth, ms (95% CI)", title_justify="left"
    )
    table.add_column("metric", no_wrap=True)
    for name in ("injected", "n", "mean error", "worst", "<= 20"):
        table.add_column(name, justify="right", no_wrap=True)
    for index, check in enumerate(checks):
        table.add_row(
            Text(check.metric),
            str(check.injected_ms),
            str(len(check.errors_ms)),
            _ms(check.mean_error(seed + index)) if check.errors_ms else "no measurement",
            f"{check.worst_ms:.1f}" if check.errors_ms else "-",
            Text("yes", "green") if check.passed else Text("NO", "bold red"),
        )
    stdout.print(table)
    passed = voice_calibration.all_passed(checks)
    stdout.print(
        Text.assemble(
            "Voice loopback: ",
            ("passed", "bold green")
            if passed
            else ("FAILED, timing off by more than 20 ms", "bold red"),
        )
    )
    return passed


def _ms(estimate: Estimate) -> str:
    return f"{estimate.value:+.1f} [{estimate.ci_low:+.1f}, {estimate.ci_high:+.1f}]"


# --- judges -------------------------------------------------------------------------------------


def _report_judges(
    project: Project, labels_path: Path, refs: Sequence[str], labelers: Sequence[str]
) -> bool:
    labels = _read_labels(labels_path, labelers)
    with config_errors(), open_store(project) as store:
        grades = [
            grade
            for ref in refs
            for grade in load_results(project, resolve_run(store, ref).id).grades
            if grade.grader.startswith(JUDGE_PREFIX) and grade.status == "ok"
        ]
    calibrations = _calibrations(project, grades, labels)
    if not calibrations:
        raise fail(
            f"no label in {labels_path} matches a judge grade of the runs {', '.join(refs)}",
            exit_codes.CONFIG_ERROR,
        )
    stdout.print(_judge_table(calibrations))
    _warn_if_not_human(labels, calibrations)
    return all(c.trusted for c in calibrations)


def _report_judge_set(
    project: Project,
    root: Path,
    labels_path: Path | None,
    labelers: Sequence[str],
    providers: Mapping[str, LLMProvider],
) -> bool:
    """Judge every labeled (case, rubric) pair of the set at ``root``, then compare with labels.

    Every rubric of the set must be calibrated: one without a usable label of its current
    version, or whose judge answered nothing usable, fails the self-test.
    """
    try:
        calibration_set = judge_calibration.load_set(root)
        labels = _read_labels(labels_path or calibration_set.labels_path, labelers)
    except CalibrationSetError as exc:
        raise fail(str(exc), exit_codes.CONFIG_ERROR) from exc
    judge_model = project.config.models.judge
    pairs = sum(1 for _ in calibration_set.items())
    stdout.print(
        f"Judging {pairs} labeled (conversation, rubric) pairs of {root} with {judge_model}.",
        soft_wrap=True,
    )
    with config_errors():
        try:
            grades = anyio.run(_judge_set, project, calibration_set, providers)
        except CalibrationSetError as exc:
            raise fail(str(exc), exit_codes.CONFIG_ERROR) from exc
    usable = [grade for grade in grades if grade.status == "ok"]
    calibrations = _calibrations(project, usable, labels)
    if calibrations:
        stdout.print(_judge_table(calibrations))
    uncalibrated = _uncalibrated(grades, calibrations)
    for grader, problem in uncalibrated.items():
        stdout.print(f"[bold red]{grader}[/]: not calibrated, {problem}", soft_wrap=True)
    errors = Counter(grade.grader for grade in grades if grade.status == "error")
    for grader, count in sorted(errors.items()):
        stdout.print(f"[yellow]{grader}[/]: {count} judge answer(s) unusable, left out")
    spent = sum((grade.cost_usd for grade in grades), Decimal(0))
    stdout.print(f"Judge spend: ${spent:.4f}")
    _warn_if_not_human(labels, calibrations)
    return bool(calibrations) and not uncalibrated and all(c.trusted for c in calibrations)


async def _judge_set(
    project: Project, calibration_set: CalibrationSet, providers: Mapping[str, LLMProvider]
) -> list[Grade]:
    config = project.config
    # Fail fast on a missing API key, before hundreds of judge calls each become an error.
    await check_model_providers(config.models, [config.models.judge], providers)
    async with open_llm(config, project.output_dir, providers=providers) as llm:
        return await judge_calibration.judge_set(calibration_set, llm, config.models.judge)


def _uncalibrated(
    grades: Sequence[Grade], calibrations: Sequence[JudgeCalibration]
) -> dict[str, str]:
    """Why each judged rubric has no calibration, by grader name."""
    calibrated = {c.grader for c in calibrations}
    versions = {grade.grader: grade.grader_version for grade in grades}
    usable = {grade.grader for grade in grades if grade.status == "ok"}
    return {
        grader: (
            f"no label of its current rubric version {versions[grader]} (relabel the set: "
            "calibration/judges/GUIDE.md)"
            if grader in usable
            else "every judge answer was unusable"
        )
        for grader in sorted(versions.keys() - calibrated)
    }


def _calibrations(
    project: Project, grades: Sequence[Grade], labels: Sequence[HumanLabel]
) -> list[JudgeCalibration]:
    """Agreement of every judge with the labels of its rubric version, where any label joins."""
    stats = project.config.stats
    seed = stats.seed if stats.seed is not None else project.config.seed
    calibrations = []
    for group in _judge_groups(grades, labels):
        try:
            calibrations.append(
                calibrate_judge(group, labels, seed=seed, resamples=stats.bootstrap_resamples)
            )
        except ValueError:
            # The rubric version was labeled, but on other trials than these grades judged.
            continue
    return calibrations


def _warn_if_not_human(
    labels: Sequence[HumanLabel], calibrations: Sequence[JudgeCalibration]
) -> None:
    drafts = Counter(
        label.labeler
        for label in labels
        if not label.labeler.startswith(judge_calibration.HUMAN_PREFIX)
    )
    if calibrations and drafts:
        names = ", ".join(sorted(drafts))
        stdout.print(
            f"[yellow]note[/]: {sum(drafts.values())} of {len(labels)} labels are not from "
            f"'{judge_calibration.HUMAN_PREFIX}' labelers ({names}); agreement with them is not "
            "evidence of agreement with humans (SC7). Use --labeler human: once people have "
            "reviewed the labels.",
            soft_wrap=True,
        )


def _read_labels(path: Path, labelers: Sequence[str]) -> list[HumanLabel]:
    try:
        labels = judge_calibration.read_labels(path)
    except CalibrationSetError as exc:
        raise fail(str(exc), exit_codes.CONFIG_ERROR) from exc
    return judge_calibration.by_labeler(labels, labelers)


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
