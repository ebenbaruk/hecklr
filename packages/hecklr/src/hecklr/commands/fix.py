"""``hecklr fix``: prompt patches for a failure cluster, kept only when validated (US-10).

The target is a cluster the miner found (``hecklr fix CLUSTER``) or what failed in a run
(``hecklr fix --from-run RUN [--baseline RUN] [--tag T] [--scenario S]``, the "regression -> fix"
flow). The fixer runs every candidate in its own copy of the project with its own agent process,
started from ``--agent-cmd`` (ADR 0015a). Exit codes: 0 a validated patch, 1 none (no candidate
beat the baseline, or nothing to fix), 3 a run or the agent failed, 4 budget, 78 configuration.
"""

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Annotated

import anyio
import typer
from rich.markup import escape

from hecklr import exit_codes
from hecklr.commands._common import (
    CONFIG_ERRORS,
    DEFAULT_CONFIG,
    ConfigPath,
    config_errors,
    fail,
    load_project_or_exit,
    overrides,
    stderr,
    stdout,
)
from hecklr.compose import RunRequest, execute_run, prepare_run
from hecklr.results import definitions, load_results, open_store, resolve_run
from hecklr_core import Project, open_llm
from hecklr_fix import (
    FIX_DIR,
    AgentCommand,
    Fixer,
    FixError,
    FixOutcome,
    FixResult,
    FixSettings,
    FixTarget,
    PassRequest,
    SuiteRun,
    load_editable,
    mined_target,
    open_pull_request,
    render_markdown,
    target_from_run,
    write_session,
)
from hecklr_llm import LLMError
from hecklr_schemas import LLMProvider, new_id

_EXIT = {
    FixOutcome.FIXED: exit_codes.OK,
    FixOutcome.NO_CANDIDATE_BEAT_BASELINE: exit_codes.CHECK_FAILED,
    FixOutcome.BASELINE_PASSES: exit_codes.CHECK_FAILED,
    FixOutcome.BUDGET_EXHAUSTED: exit_codes.BUDGET,
}


def fix(
    ctx: typer.Context,
    cluster: Annotated[
        str | None, typer.Argument(help="Cluster id from `hecklr mine` (or use --from-run).")
    ] = None,
    agent_cmd: Annotated[
        str,
        typer.Option(
            help="Starts the agent from a project copy: {port} (required), {python}, {project}; "
            "e.g. '{python} agent/serve.py --port {port}'.",
        ),
    ] = "",
    config: ConfigPath = DEFAULT_CONFIG,
    from_run: Annotated[
        str | None, typer.Option(help="Fix what failed in this run: an id or `latest`.")
    ] = None,
    baseline: Annotated[
        str | None,
        typer.Option(help="With --from-run: only scenarios whose pass rate dropped vs this run."),
    ] = None,
    tag: Annotated[
        list[str] | None, typer.Option(help="With --from-run: only scenarios with this tag.")
    ] = None,
    scenario: Annotated[
        list[str] | None, typer.Option(help="With --from-run: only this scenario (repeatable).")
    ] = None,
    candidates: Annotated[int, typer.Option(min=1, help="Patches to generate.")] = 5,
    trials: Annotated[
        int, typer.Option(min=1, help="Trials per cluster scenario in the targeted pass.")
    ] = 3,
    guard_suite: Annotated[str, typer.Option(help="Suite the guard pass runs.")] = "ci",
    guard_trials: Annotated[
        int | None, typer.Option(min=1, help="Trials per guard scenario [default: the suite's].")
    ] = None,
    model: Annotated[
        str | None, typer.Option(help="Model that writes the patches [default: models.judge].")
    ] = None,
    seed: Annotated[int | None, typer.Option(help="Seed of every run [default: seed].")] = None,
    budget_usd: Annotated[
        float | None,
        typer.Option(min=0, help="Cap on Hecklr's spend for the session [default: budget.usd]."),
    ] = None,
    ready_timeout: Annotated[
        float, typer.Option(min=1, help="Seconds the agent has to start listening.")
    ] = 60.0,
    pr: Annotated[bool, typer.Option("--pr", help="Open a pull request with the winner.")] = False,
    draft: Annotated[bool, typer.Option(help="With --pr: open it as a draft.")] = False,
) -> None:
    """Generate prompt patches for a failure cluster; keep one only if it is validated."""
    project = load_project_or_exit(config)
    try:
        command = AgentCommand(_required_agent_cmd(agent_cmd), ready_timeout_s=ready_timeout)
        editable = load_editable(project.config, project.config_path)
        if guard_suite not in project.config.suites:
            raise FixError(f"--guard-suite: hecklr.yaml has no suite {guard_suite!r}")
        target = _target(project, cluster, from_run, baseline, tag or [], scenario or [])
    except FixError as exc:
        raise fail(str(exc), exit_codes.CONFIG_ERROR) from exc
    budget = Decimal(str(budget_usd)) if budget_usd is not None else project.config.budget.usd
    settings = FixSettings(
        model=model or project.config.models.judge,
        guard_suite=guard_suite,
        seed=seed if seed is not None else project.config.seed,
        stats=project.config.stats,
        candidates=candidates,
        targeted_trials=trials,
        guard_trials=guard_trials,
        budget_usd=budget,
    )
    fix_root = project.output_dir / FIX_DIR
    fix_id = new_id()
    session_dir = fix_root / fix_id
    providers = overrides(ctx).llm_providers

    def make_fixer(llm: LLMProvider) -> Fixer:
        return Fixer(
            config_path=project.config_path,
            editable=editable,
            target=target,
            settings=settings,
            llm=llm,
            runner=partial(_run_pass, providers),
            launcher=command,
            session_dir=session_dir,
            runs_output_dir=fix_root,
            fix_id=fix_id,
        )

    stderr.print(
        f"Fixing [bold]{escape(target.cluster.label)}[/] ({len(target.scenarios)} scenarios); "
        f"session {fix_id}",
        soft_wrap=True,
    )
    result = _run(project, fix_root, make_fixer, providers)
    report = render_markdown(result, guard_suite=guard_suite)
    write_session(session_dir, result, guard_suite=guard_suite, report=report)
    typer.echo(report)
    _print_paths(project, session_dir, result)
    if pr:
        _open_pr(project, result, report, draft)
    raise typer.Exit(code=_EXIT[result.outcome])


def _required_agent_cmd(agent_cmd: str) -> str:
    if not agent_cmd.strip():
        raise FixError(
            "--agent-cmd is required: the fixer starts the agent itself for every candidate, "
            "e.g. --agent-cmd '{python} agent/serve.py --port {port}' (ADR 0015a)"
        )
    return agent_cmd


def load_mined_target(output_dir: Path, cluster_id: str) -> FixTarget:
    """``hecklr fix CLUSTER``'s target, read from the miner's store under ``output_dir``.

    Raises:
        FixError: the miner is not installed, its store cannot be read, or the cluster has
            nothing to run.
    """
    try:
        # Imported here: the miner is an optional extra, and it loads scikit-learn, which every
        # other command would pay for at start-up (as in ``hecklr.commands.mine``).
        import hecklr_mine
    except ImportError as exc:
        raise FixError(
            "hecklr fix CLUSTER reads the miner's clusters: pip install 'hecklr[mine]'"
        ) from exc
    mine_root = output_dir / hecklr_mine.MINE_DIR
    try:
        clusters = hecklr_mine.load_clusters(mine_root)
        proposals = hecklr_mine.load_queue(mine_root)
    except hecklr_mine.MineError as exc:
        raise FixError(str(exc)) from exc
    return mined_target(clusters, proposals, cluster_id)


def _target(
    project: Project,
    cluster: str | None,
    from_run: str | None,
    baseline: str | None,
    tags: Sequence[str],
    scenarios: Sequence[str],
) -> FixTarget:
    if (cluster is None) == (from_run is None):
        raise FixError("name exactly one target: a CLUSTER id or --from-run RUN")
    if cluster is not None:
        if baseline or tags or scenarios:
            raise FixError("--baseline, --tag and --scenario go with --from-run")
        return load_mined_target(project.output_dir, cluster)
    assert from_run is not None  # noqa: S101 -- exactly one target, checked above
    with config_errors(), open_store(project) as store:
        run = resolve_run(store, from_run)
        base = resolve_run(store, baseline) if baseline is not None else None
    if run.suite is None or run.suite not in project.config.suites:
        raise FixError(f"run {run.id} ran suite {run.suite!r}, which hecklr.yaml no longer has")
    with config_errors():
        defs = definitions(project, [run.suite])
        results = load_results(project, run.id)
        base_results = load_results(project, base.id) if base is not None else None
    suite = project.config.suites[run.suite]
    return target_from_run(
        results,
        defs.scenarios,
        defs.personas,
        baseline=base_results,
        tags=tags,
        scenario_ids=scenarios,
        channels=suite.channels,
        personas_glob=suite.personas,
    )


def _run(
    project: Project,
    fix_root: Path,
    make_fixer: Callable[[LLMProvider], Fixer],
    providers: Mapping[str, LLMProvider],
) -> FixResult:
    async def session() -> FixResult:
        async with open_llm(project.config, fix_root, providers=providers or None) as llm:
            return await make_fixer(llm).run()

    try:
        return anyio.run(session)
    except CONFIG_ERRORS as exc:
        raise fail(str(exc), exit_codes.CONFIG_ERROR) from exc
    except (FixError, LLMError) as exc:
        raise fail(f"the fix session failed: {exc}", exit_codes.HARNESS_ERROR) from exc


async def _run_pass(providers: Mapping[str, LLMProvider], request: PassRequest) -> SuiteRun:
    """One pass, as ``hecklr run`` would run it, into the fixer's own store."""
    prepared = prepare_run(
        RunRequest(
            config_path=request.config_path,
            suite=request.suite,
            trials=request.trials,
            budget_usd=request.budget_usd,
            seed=request.seed,
            output_dir=request.output_dir,
            agent_version=request.config_path.parent.name,
        )
    )
    label = request.config_path.parent.name
    stderr.print(f"  {label}: {len(prepared.plan.run.matrix)} trials, run {prepared.plan.run.id}")
    results = await execute_run(prepared, llm_providers=providers or None)
    suite = prepared.plan.suite
    return SuiteRun(results=results, scenarios=suite.scenarios, personas=suite.personas)


def _print_paths(project: Project, session_dir: Path, result: FixResult) -> None:
    stdout.print(f"Session: {escape(str(session_dir))}", soft_wrap=True)
    if result.winner is not None:
        diff = session_dir / "winner.diff"
        stdout.print(
            f"Apply: patch -p1 -d {escape(str(project.root))} < {escape(str(diff))}",
            soft_wrap=True,
        )


def _open_pr(project: Project, result: FixResult, report: str, draft: bool) -> None:
    if result.winner is None:
        stderr.print("No pull request: no candidate was validated.")
        return
    title = f"fix(agent): {result.cluster.label}"
    try:
        opened = open_pull_request(
            project.root,
            result.winner_files,
            branch=f"hecklr-fix/{result.fix_id.lower()}",
            title=title if len(title) <= 72 else title[:71] + "…",
            body=report,
            draft=draft,
        )
    except FixError as exc:
        raise fail(f"--pr: {exc}", exit_codes.HARNESS_ERROR) from exc
    stdout.print(f"Pull request: {opened.url} ({opened.branch} -> {opened.base})", soft_wrap=True)
