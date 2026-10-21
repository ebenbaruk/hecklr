"""``hecklr init``: write a starter project that runs on the first ``hecklr run`` (SC1, G1)."""

import os
import re
from pathlib import Path
from typing import Annotated

import typer
from pydantic import TypeAdapter, ValidationError
from rich.markup import escape

from hecklr import exit_codes
from hecklr.commands._common import CONFIG_ERRORS, fail, stdout
from hecklr.compose import load_run_project
from hecklr.probe import ollama_models, probe_agent, reply_path
from hecklr.scaffold import (
    CONFIG_FILE,
    AdapterType,
    AgentChoice,
    ModelChoice,
    choose_models,
    existing_files,
    write_project,
)
from hecklr_core import resolve_suite
from hecklr_schemas.common import Identifier, ModelName

_DEFAULT_AGENT_NAME = "my-agent"
_NAME_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]+")
_IDENTIFIER: TypeAdapter[str] = TypeAdapter(Identifier)
_MODEL_NAME: TypeAdapter[str] = TypeAdapter(ModelName)


def init(
    directory: Annotated[
        Path, typer.Argument(help="Project directory; created if missing.")
    ] = Path(),
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Accept every default; never prompt.")
    ] = False,
    agent_url: Annotated[
        str | None, typer.Option(help="Agent endpoint; probed to pick the adapter.")
    ] = None,
    adapter: Annotated[
        AdapterType | None,
        typer.Option(help="Adapter type; `python` writes an in-process starter agent."),
    ] = None,
    agent_model: Annotated[
        str | None, typer.Option(help="Model name an openai-compatible agent expects.")
    ] = None,
    agent_name: Annotated[str | None, typer.Option(help="Agent name in hecklr.yaml.")] = None,
    simulator_model: Annotated[
        str | None, typer.Option(help="Simulator model, provider/model.")
    ] = None,
    judge_model: Annotated[str | None, typer.Option(help="Judge model, provider/model.")] = None,
    force: Annotated[bool, typer.Option(help="Overwrite existing starter files.")] = False,
) -> None:
    """Interactive setup: write hecklr.yaml, a starter world, scenarios and personas."""
    root = directory.resolve()
    choice = _agent_choice(agent_url, adapter, agent_model, yes=yes)
    models = _models(simulator_model, judge_model, yes=yes)
    clashes = existing_files(root, choice)
    if clashes and not force:
        names = ", ".join(str(path.relative_to(root)) for path in clashes)
        raise fail(
            f"{root} already has {names}; pass --force to overwrite", exit_codes.CONFIG_ERROR
        )
    name = agent_name or _agent_name(root)
    _check(_IDENTIFIER, name, "--agent-name")
    _check(_MODEL_NAME, models.simulator, "simulator model")
    _check(_MODEL_NAME, models.judge, "judge model")
    written = write_project(root, name, choice, models)
    try:
        # Proves the written project loads and plans, so `hecklr run` cannot fail on it.
        resolve_suite(load_run_project(root / CONFIG_FILE), "ci")
    except CONFIG_ERRORS as exc:
        raise fail(
            f"the project written to {root} does not load: {exc}. Fix the file named above, or "
            "re-run `hecklr init` with --force to overwrite the files it wrote",
            exit_codes.CONFIG_ERROR,
        ) from exc
    stdout.print(f"Wrote {len(written)} files to {escape(str(root))}:", soft_wrap=True)
    for path in written:
        stdout.print(f"  {escape(str(path.relative_to(root)))}")
    stdout.print(
        f"Models: simulator {models.simulator}, judge {models.judge}; {models.reason}.",
        soft_wrap=True,
    )
    location = "" if root == Path.cwd() else f"cd {escape(str(root))} && "
    stdout.print(f"Next: [bold]{location}hecklr doctor && hecklr run[/]", soft_wrap=True)


def _agent_choice(
    url: str | None, adapter: AdapterType | None, model: str | None, *, yes: bool
) -> AgentChoice:
    if adapter == "python":
        if url is not None:
            raise fail(
                "--adapter python runs an in-process agent and takes no --agent-url; "
                "drop one of the two",
                exit_codes.CONFIG_ERROR,
            )
        return AgentChoice("python")
    if url is None and adapter is None and not yes:
        url = (
            typer.prompt(
                "Agent URL (blank for an in-process starter agent)", default="", show_default=False
            ).strip()
            or None
        )
    if url is None:
        if adapter is not None:
            raise fail(f"--adapter {adapter} needs --agent-url", exit_codes.CONFIG_ERROR)
        return AgentChoice("python")
    if adapter is not None:
        return AgentChoice(adapter, url, model)
    probe = probe_agent(url)
    style = "green" if probe.reachable else "yellow"
    stdout.print(f"[{style}]{escape(probe.detail)}[/]: using the {probe.adapter} adapter.")
    if not probe.reachable:
        stdout.print("Start the agent before `hecklr run`; `hecklr doctor` checks it.")
        return AgentChoice(probe.adapter, probe.url, model)
    if probe.adapter == "openai-compatible":
        return AgentChoice(probe.adapter, probe.url, model or next(iter(probe.models), None))
    path = reply_path(probe.url)
    if path is not None:
        stdout.print(f"The agent answers in the JSON field [bold]{escape(path)}[/].")
    return AgentChoice(probe.adapter, probe.url, response_path=path)


def _models(simulator: str | None, judge: str | None, *, yes: bool) -> ModelChoice:
    if simulator is not None and judge is not None:
        return ModelChoice(simulator, judge, "chosen on the command line")
    detected = choose_models(os.environ, ollama_models())
    chosen = ModelChoice(simulator or detected.simulator, judge or detected.judge, detected.reason)
    if yes:
        return chosen
    return ModelChoice(
        typer.prompt("Simulator model", default=chosen.simulator),
        typer.prompt("Judge model", default=chosen.judge),
        chosen.reason,
    )


def _agent_name(root: Path) -> str:
    name = _NAME_CHARACTERS.sub("-", root.name).strip("-._")
    return name or _DEFAULT_AGENT_NAME


def _check(kind: TypeAdapter[str], value: str, what: str) -> None:
    try:
        kind.validate_python(value)
    except ValidationError as exc:
        message = exc.errors()[0]["msg"]
        raise fail(f"invalid {what} {value!r}: {message}", exit_codes.CONFIG_ERROR) from exc
