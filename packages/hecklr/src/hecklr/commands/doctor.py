"""``hecklr doctor``: check that a run can start, before spending a minute finding out it cannot.

Checks the Python version, optional extras, the project files, model provider keys (by name;
values are never printed), Ollama, the local ports the harness binds and the agent itself.
"""

import importlib.util
import os
import socket
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal
from urllib.parse import urlsplit

import anyio
import typer
from rich.markup import escape
from rich.table import Table

from hecklr import exit_codes
from hecklr.commands._common import CONFIG_ERRORS, stdout
from hecklr.compose import load_run_project
from hecklr.probe import DEFAULT_OLLAMA_URL, ollama_models, probe_agent
from hecklr_adapters import AdapterConfigError, load_agent
from hecklr_core import Project
from hecklr_llm import LLMError, ProviderRouter, split_model
from hecklr_schemas import (
    HttpAdapterConfig,
    OpenAICompatibleAdapterConfig,
    PythonAdapterConfig,
    WebhookAdapterConfig,
    WebSocketAdapterConfig,
)

MIN_PYTHON: Final = (3, 12)
_EXTRAS: Final = {"voice": "hecklr_voice", "mine": "hecklr_mine"}

Status = Literal["ok", "warn", "fail"]
_STYLE: Final = {"ok": "green", "warn": "yellow", "fail": "red"}


@dataclass(frozen=True, slots=True)
class Check:
    """One doctor finding."""

    name: str
    status: Status
    detail: str


def doctor(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to hecklr.yaml.")] = Path(
        "hecklr.yaml"
    ),
) -> None:
    """Check keys, audio dependencies, ports and agent reachability."""
    checks = list(run_checks(config))
    table = Table(title="hecklr doctor", title_justify="left")
    for column in ("check", "status", "detail"):
        table.add_column(column)
    for check in checks:
        style = _STYLE[check.status]
        table.add_row(escape(check.name), f"[{style}]{check.status}[/]", escape(check.detail))
    stdout.print(table)
    failed = sum(check.status == "fail" for check in checks)
    if failed:
        stdout.print(f"[bold red]{failed} check(s) failed.[/]")
        raise typer.Exit(code=exit_codes.CHECK_FAILED)
    stdout.print("[bold green]Ready to run.[/]")


def run_checks(config_path: Path) -> Iterator[Check]:
    """Every check, in the order a user would fix them."""
    yield _python_version()
    yield from _extras()
    yield _local_port()
    try:
        project = load_run_project(config_path)
    except (*CONFIG_ERRORS, OSError) as exc:
        yield Check("config", "fail", str(exc))
        return
    yield Check("config", "ok", f"{config_path} with {len(project.config.suites)} suite(s)")
    yield from _model_keys(project)
    yield from _ollama(project)
    yield from _agent(project)


def _python_version() -> Check:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info[:2] < MIN_PYTHON:
        return Check("python", "fail", f"Python {version}; Hecklr needs 3.12 or newer")
    return Check("python", "ok", f"Python {version}")


def _extras() -> Iterator[Check]:
    for extra, module in _EXTRAS.items():
        if importlib.util.find_spec(module) is None:
            yield Check(f"extra [{extra}]", "warn", f'not installed: pip install "hecklr[{extra}]"')
        else:
            yield Check(f"extra [{extra}]", "ok", "installed")


def _local_port() -> Check:
    # The world sandbox serves every trial's tools from an ephemeral port on 127.0.0.1.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
    except OSError as exc:
        return Check("ports", "fail", f"cannot bind a local port for the world sandbox: {exc}")
    return Check("ports", "ok", "the world sandbox can bind 127.0.0.1")


def _model_keys(project: Project) -> Iterator[Check]:
    models = project.config.models
    router = ProviderRouter(models.providers, prices=models.prices)
    try:
        for role, model in (("simulator", models.simulator), ("judge", models.judge)):
            try:
                router.provider_for(model)
            except LLMError as exc:
                yield Check(f"{role} model", "fail", f"{model}: {exc}")
            else:
                yield Check(f"{role} model", "ok", f"{model}: provider configured")
    finally:
        anyio.run(router.aclose)


def _ollama(project: Project) -> Iterator[Check]:
    models = project.config.models
    wanted = sorted(
        {
            split_model(m)[1]
            for m in (models.simulator, models.judge)
            if split_model(m)[0] == "ollama"
        }
    )
    if not wanted:
        return
    settings = models.providers.get("ollama")
    configured = str(settings.base_url) if settings and settings.base_url else None
    base = (
        (configured or os.environ.get("OLLAMA_BASE_URL") or DEFAULT_OLLAMA_URL)
        .removesuffix("/")
        .removesuffix("/v1")
    )
    served = ollama_models(base)
    if served is None:
        yield Check("ollama", "fail", f"no Ollama at {base}; start it with `ollama serve`")
        return
    missing = [model for model in wanted if model not in served]
    if missing:
        pulls = " && ".join(f"ollama pull {model}" for model in missing)
        yield Check("ollama", "fail", f"models not pulled: {', '.join(missing)}; run {pulls}")
    else:
        yield Check("ollama", "ok", f"{base} serves {', '.join(wanted)}")


def _agent(project: Project) -> Iterator[Check]:
    chat = project.config.agent.adapters.chat
    if chat is None:
        return
    match chat:
        case HttpAdapterConfig() | OpenAICompatibleAdapterConfig():
            probe = probe_agent(str(chat.url))
            yield Check("agent (chat)", "ok" if probe.reachable else "fail", probe.detail)
        case WebSocketAdapterConfig():
            yield _tcp_check(chat.url)
        case PythonAdapterConfig():
            yield _python_agent(project, chat.entrypoint)
        case WebhookAdapterConfig():
            yield Check("agent (chat)", "fail", "the webhook chat adapter is not available yet")
    for channel in ("voice", "email"):
        if getattr(project.config.agent.adapters, channel) is not None:
            yield Check(
                f"agent ({channel})", "warn", f"the {channel} channel driver is not available yet"
            )


def _tcp_check(url: str) -> Check:
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "wss" else 80)
    host = parts.hostname or "localhost"
    try:
        socket.create_connection((host, port), timeout=3).close()
    except OSError as exc:
        return Check("agent (chat)", "fail", f"nothing listens at {host}:{port}: {exc}")
    return Check("agent (chat)", "ok", f"{host}:{port} accepts connections")


def _python_agent(project: Project, entrypoint: str) -> Check:
    root = str(project.root)
    added = root not in sys.path
    if added:
        sys.path.insert(0, root)
    try:
        load_agent(entrypoint)
    except AdapterConfigError as exc:
        return Check("agent (chat)", "fail", str(exc))
    finally:
        if added:
            sys.path.remove(root)
    return Check("agent (chat)", "ok", f"in-process agent {entrypoint} imports")
