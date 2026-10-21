"""``hecklr replay FILE``: real transcripts become adaptive replay scenarios (US-02)."""

from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import Annotated

import anyio
import typer
from rich.markup import escape

from hecklr import exit_codes
from hecklr.commands._common import config_errors, fail, overrides, stdout
from hecklr.compose import load_run_project
from hecklr_core import PERSONA_LIBRARY_GLOB, Project, load_model, open_llm
from hecklr_schemas import LLMProvider, Persona
from hecklr_sim import ReplayBatch, transcripts_to_scenarios


def replay(
    ctx: typer.Context,
    transcripts: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, help="JSONL file, one conversation a line."),
    ],
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to hecklr.yaml.")] = Path(
        "hecklr.yaml"
    ),
    out: Annotated[
        Path | None,
        typer.Option(help="Directory for the scenario files [default: scenarios/replay]."),
    ] = None,
    persona: Annotated[
        str | None,
        typer.Option(help="Persona the replays run with [default: the first in personas/]."),
    ] = None,
    model: Annotated[
        str | None, typer.Option(help="Extraction model [default: models.simulator].")
    ] = None,
) -> None:
    """Convert real transcripts into replay scenarios."""
    with config_errors():
        project = load_run_project(config)
        persona_id = persona or _first_persona(project)
    out_dir = out if out is not None else project.root / "scenarios" / "replay"
    try:
        batch = anyio.run(
            partial(
                _extract,
                project,
                transcripts,
                out_dir,
                persona_id,
                model or project.config.models.simulator,
                overrides(ctx).llm_providers,
            )
        )
    except (ValueError, FileExistsError) as exc:
        raise fail(str(exc), exit_codes.CONFIG_ERROR) from exc
    for path in batch.written:
        stdout.print(f"wrote {escape(str(path))}")
    for failure in batch.failures:
        stdout.print(
            f"[yellow]skipped {escape(failure.conversation_id)}[/]: {escape(failure.error)}"
        )
    stdout.print(
        f"{len(batch.written)} replay scenario(s) written, {len(batch.failures)} skipped; "
        "review their goals and success criteria before running them."
    )
    if batch.failures:
        raise typer.Exit(code=exit_codes.CHECK_FAILED)


async def _extract(
    project: Project,
    transcripts: Path,
    out_dir: Path,
    persona: str,
    model: str,
    providers: Mapping[str, LLMProvider],
) -> ReplayBatch:
    async with open_llm(project.config, project.output_dir, providers=providers or None) as llm:
        return await transcripts_to_scenarios(
            transcripts,
            out_dir,
            llm,
            model=model,
            world=project.world.id,
            persona=persona,
            seed=project.config.seed,
        )


def _first_persona(project: Project) -> str:
    paths = sorted(project.root.glob(PERSONA_LIBRARY_GLOB))
    if not paths:
        raise fail(
            f"no persona in {project.root / 'personas'}; pass --persona", exit_codes.CONFIG_ERROR
        )
    return load_model(paths[0], Persona).id
