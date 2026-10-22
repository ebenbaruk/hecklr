"""``hecklr mine LOGS``: production logs become failure clusters and proposed scenarios (US-08).

The miner lives in the optional ``hecklr-mine`` package (``pip install 'hecklr[mine]'``), so it is
imported when the command runs, not when the CLI starts.
"""

import os
from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import anyio
import typer
from rich.markup import escape
from rich.table import Table

from hecklr import exit_codes
from hecklr.commands._common import (
    CONFIG_ERRORS,
    DEFAULT_CONFIG,
    ConfigPath,
    fail,
    load_project_or_exit,
    overrides,
    stderr,
    stdout,
)
from hecklr_core import PERSONA_LIBRARY_GLOB, Project, load_model, open_llm
from hecklr_llm import LLMError
from hecklr_schemas import Conversation, LLMProvider, Persona, Scenario, new_id

if TYPE_CHECKING:
    from hecklr_mine import Embedder, MineResult, MineSettings, MineSummary, Scrubber


class LogFormatOption(StrEnum):
    """``--format`` values; mirrors ``hecklr_mine.LogFormat``."""

    JSONL = "jsonl"
    OTEL = "otel"
    LANGFUSE = "langfuse"
    LANGSMITH = "langsmith"
    CSV = "csv"


class EmbedderOption(StrEnum):
    """``--embedder`` values."""

    HASHING = "hashing"
    """Free and offline."""
    OPENAI = "openai"
    """``text-embedding-3-small``; needs the OpenAI key."""


def mine(
    ctx: typer.Context,
    logs: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Log export to mine.")],
    log_format: Annotated[
        LogFormatOption, typer.Option("--format", "-f", help="Format of LOGS.")
    ] = LogFormatOption.JSONL,
    mapping: Annotated[
        Path | None,
        typer.Option(exists=True, dir_okay=False, help="CSV column mapping (YAML), for csv."),
    ] = None,
    config: ConfigPath = DEFAULT_CONFIG,
    out: Annotated[
        Path | None,
        typer.Option(
            help="Directory for the results [default: <output_dir>/mine/<mine id>, where the "
            "review queue looks]."
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(help="Model for labels and scenarios [default: models.simulator]."),
    ] = None,
    no_model: Annotated[
        bool, typer.Option("--no-model", help="No LLM: keyword labels, no proposed scenarios.")
    ] = False,
    embedder: Annotated[
        EmbedderOption, typer.Option(help="How failure summaries are embedded.")
    ] = EmbedderOption.HASHING,
    judge: Annotated[
        list[str] | None,
        typer.Option(help="Judge every conversation on this rubric (repeatable), e.g. resolved."),
    ] = None,
    llm_scrub: Annotated[
        bool, typer.Option(help="Also ask the model for PII the patterns missed (names).")
    ] = False,
    llm_summaries: Annotated[
        bool, typer.Option(help="Cluster LLM-written root causes instead of transcript text.")
    ] = False,
    presidio: Annotated[
        bool, typer.Option(help="Also scrub with Presidio (hecklr-mine[presidio]).")
    ] = False,
    persona: Annotated[
        str | None,
        typer.Option(
            help="Persona of the proposed scenarios [default: first non-adversarial persona]."
        ),
    ] = None,
    min_cluster_size: Annotated[
        int | None, typer.Option(min=2, help="Smallest cluster [default: 2.5% of failures].")
    ] = None,
    dedup_threshold: Annotated[
        float, typer.Option(min=0.0, max=1.0, help="Similarity that makes a duplicate.")
    ] = 0.9,
    budget_usd: Annotated[
        float | None,
        typer.Option(
            min=0.0,
            help="Spending cap [default: budget.usd]. Checked before each model call, so the "
            "calls already in flight (up to 8) can overshoot it slightly.",
        ),
    ] = None,
) -> None:
    """Mine production logs into failure clusters and proposed scenarios."""
    try:
        import hecklr_mine
    except ImportError as exc:
        raise fail(
            "hecklr mine needs the miner: pip install 'hecklr[mine]'", exit_codes.CONFIG_ERROR
        ) from exc
    project = load_project_or_exit(config)
    if not (presidio or llm_scrub):
        stderr.print(
            "[yellow]warning[/]: person names are kept: the default scrubber only finds patterns "
            "(e-mails, phones, addresses, cards, IBANs). Add --presidio or --llm-scrub to remove "
            "names."
        )
    chosen_model = None if no_model else (model or project.config.models.simulator)
    cap = Decimal(str(budget_usd)) if budget_usd is not None else project.config.budget.usd
    mine_root = project.output_dir / hecklr_mine.MINE_DIR
    mine_id = new_id()
    target = out if out is not None else mine_root / mine_id
    try:
        settings = hecklr_mine.MineSettings(
            world=project.world.id,
            persona=_persona(project, persona),
            model=chosen_model,
            judge_model=project.config.models.judge if judge else None,
            rubrics=tuple(judge or ()),
            llm_scrub=llm_scrub,
            llm_summaries=llm_summaries,
            min_cluster_size=min_cluster_size,
            dedup_threshold=dedup_threshold,
            max_usd=cap,
            seed=project.config.seed,
        )
        csv_mapping = hecklr_mine.CsvMapping.from_yaml(mapping) if mapping else None
        conversations = hecklr_mine.load_logs(
            logs, hecklr_mine.LogFormat(log_format.value), mapping=csv_mapping
        )
        existing = [*_suite_scenarios(project), *hecklr_mine.queued_scenarios(mine_root)]
        job = partial(
            _mine,
            project,
            conversations,
            settings,
            _embedder(embedder, project),
            _scrubber(presidio),
            existing,
            overrides(ctx).llm_providers,
        )
        result = anyio.run(job)
        summary = hecklr_mine.summarize(
            result,
            mine_id=mine_id,
            source=str(logs),
            log_format=log_format.value,
            model=chosen_model,
        )
        hecklr_mine.write_run(target, result, summary)
    except hecklr_mine.BudgetExceededError as exc:
        raise fail(str(exc), exit_codes.BUDGET) from exc
    except (*CONFIG_ERRORS, hecklr_mine.MineError, ValueError, FileExistsError) as exc:
        raise fail(str(exc), exit_codes.CONFIG_ERROR) from exc
    except LLMError as exc:
        raise fail(f"model call failed: {exc}", exit_codes.HARNESS_ERROR) from exc
    _print(result, summary, target)


def _persona(project: Project, requested: str | None) -> str:
    """``requested`` if the persona library has it, else its first ordinary persona.

    Mined scenarios replay real customers, so the default skips adversarial personas.
    """
    personas = [load_model(p, Persona) for p in sorted(project.root.glob(PERSONA_LIBRARY_GLOB))]
    ids = [persona.id for persona in personas]
    if requested is not None and requested not in ids:
        raise fail(
            f"no persona {requested!r} in {project.root / 'personas'}; known: {ids}",
            exit_codes.CONFIG_ERROR,
        )
    ordinary = [persona.id for persona in personas if persona.adversarial is None]
    if requested is None and not ordinary:
        raise fail(
            f"no non-adversarial persona in {project.root / 'personas'}; pass --persona",
            exit_codes.CONFIG_ERROR,
        )
    return requested or ordinary[0]


def _suite_scenarios(project: Project) -> list[Scenario]:
    """Every scenario any suite selects, once each: the suite proposals must not duplicate."""
    paths = {
        path
        for settings in project.config.suites.values()
        for path in project.root.glob(settings.scenarios)
    }
    found = {scenario.id: scenario for scenario in (load_model(p, Scenario) for p in sorted(paths))}
    return list(found.values())


def _scrubber(presidio: bool) -> "Scrubber":
    import hecklr_mine

    detectors: list[hecklr_mine.PiiDetector] = [hecklr_mine.RegexDetector()]
    if presidio:
        detectors.append(hecklr_mine.PresidioDetector.from_spacy_model())
    return hecklr_mine.Scrubber(detectors)


def _embedder(choice: EmbedderOption, project: Project) -> "Embedder":
    import hecklr_mine

    if choice is EmbedderOption.HASHING:
        return hecklr_mine.HashingEmbedder()
    settings = project.config.models.providers.get("openai")
    key_env = (settings.api_key_env if settings else None) or "OPENAI_API_KEY"
    key = os.environ.get(key_env)
    if not key:
        raise fail(f"--embedder openai needs an API key in {key_env}", exit_codes.CONFIG_ERROR)
    base_url = str(settings.base_url) if settings and settings.base_url else None
    return hecklr_mine.OpenAIEmbedder(api_key=key, base_url=base_url)


async def _mine(
    project: Project,
    conversations: Sequence[Conversation],
    settings: "MineSettings",
    embedder: "Embedder",
    scrubber: "Scrubber",
    existing: Sequence[Scenario],
    providers: Mapping[str, LLMProvider],
) -> "MineResult":
    import hecklr_mine

    try:
        if settings.model is None and settings.judge_model is None:
            return await hecklr_mine.mine(
                conversations, settings, embedder=embedder, scrubber=scrubber, existing=existing
            )
        async with open_llm(project.config, project.output_dir, providers=providers or None) as llm:
            return await hecklr_mine.mine(
                conversations,
                settings,
                embedder=embedder,
                llm=llm,
                scrubber=scrubber,
                existing=existing,
            )
    finally:
        if isinstance(embedder, hecklr_mine.OpenAIEmbedder):
            await embedder.aclose()


def _print(result: "MineResult", summary: "MineSummary", target: Path) -> None:
    signals = ", ".join(f"{kind} {count}" for kind, count in summary.signals.items()) or "none"
    stdout.print(
        f"Mined {summary.conversations} conversation(s): {summary.failures} failed "
        f"({escape(signals)})."
    )
    stdout.print(
        f"{summary.clusters} cluster(s), {summary.noise} failure(s) left as noise; PII scrubbed: "
        f"{sum(summary.placeholders.values())} distinct value(s)."
    )
    if result.clusters:
        proposed = {p.cluster_id: p.scenario.id for p in result.proposals}
        table = Table("size", "cluster", "proposed scenario")
        for cluster in result.clusters:
            table.add_row(
                str(cluster.size), escape(cluster.label), escape(proposed.get(cluster.id, "-"))
            )
        stdout.print(table)
    for duplicate in result.duplicates:
        stdout.print(
            f"[yellow]duplicate[/] {escape(duplicate.scenario_id)} ~ "
            f"{escape(duplicate.duplicate_of)} ({duplicate.similarity:.2f})"
        )
    if result.scrub_errors or result.summary_errors:
        stdout.print(
            f"[yellow]model replies unusable[/]: LLM scrub on {len(result.scrub_errors)} and "
            f"summary on {len(result.summary_errors)} conversation(s); pattern scrubbing and "
            "heuristic summaries were kept (see summary.json)."
        )
    for cluster_id, error in result.synthesis_errors.items():
        stdout.print(f"[yellow]no scenario for cluster {cluster_id}[/]: {escape(error)}")
    spend = summary.llm_usd + summary.embedding_usd
    stdout.print(
        f"{summary.proposals} scenario proposal(s) queued for review; spent ${spend:.4f}. "
        f"Results: {escape(str(target))}"
    )
