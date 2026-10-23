"""An isolated copy of the agent project for one pass of one candidate (ADR 0015a).

The user's files are never touched: each pass copies the directory holding ``hecklr.yaml``,
writes the candidate's editable files into the copy, adds the targeted suite (the cluster's
scenarios) and points every adapter URL at the port its own agent process listens on.
"""

import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final
from urllib.parse import urlsplit, urlunsplit

import yaml

from hecklr_fix.errors import FixError
from hecklr_fix.target import FixTarget
from hecklr_sim import scenario_to_yaml

TARGETED_SUITE: Final = "hecklr-fix-targeted"
"""Suite added to the copy's ``hecklr.yaml``: the cluster's scenarios."""
TARGETED_DIR: Final = ".hecklr-fix/targeted"
"""Where the copy holds the cluster's scenarios; outside every scenario glob of the user."""
NEVER_COPIED: Final = frozenset(
    {
        ".git",
        ".hecklr-fix",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)
"""Directory names skipped at any depth: history, environments and caches, not agent code."""


@dataclass(frozen=True, slots=True)
class Workspace:
    """A prepared copy."""

    root: Path
    config_path: Path
    agent_host: str
    """Host the agent is reached at (from the adapter URLs), for the readiness check."""


def prepare_workspace(
    *,
    config_path: Path,
    dest: Path,
    files: Mapping[str, str],
    port: int,
    target: FixTarget,
    targeted_trials: int,
) -> Workspace:
    """Copy the project of ``config_path`` to ``dest`` with ``files`` written over it.

    Args:
        config_path: The user's ``hecklr.yaml``.
        dest: A directory that does not exist yet.
        files: Editable files (project-relative POSIX path -> text) to write into the copy.
        port: The port the copy's agent will listen on.
        target: Its scenarios become the copy's ``hecklr-fix-targeted`` suite.
        targeted_trials: Trials per scenario of that suite.

    Raises:
        FixError: ``hecklr.yaml`` is unreadable or no adapter is reached by URL.
    """
    root = config_path.resolve().parent
    raw = _read_config(config_path)
    skipped_output = _top_level_name(raw.get("output_dir", ".hecklr"))
    host = _point_adapters_at(raw, port)
    suites = raw.get("suites")
    if not isinstance(suites, dict):
        raise FixError(f"{config_path} has no suites mapping")
    suites[TARGETED_SUITE] = _targeted_suite(target, targeted_trials)

    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped = {name for name in names if name in NEVER_COPIED}
        if Path(directory).resolve() == root and skipped_output in names:
            skipped.add(skipped_output)
        return skipped

    shutil.copytree(root, dest, ignore=ignore, symlinks=True)
    for relative, text in files.items():
        path = dest / relative
        if path.is_symlink():
            path.unlink()  # never write through a link into the user's tree
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    targeted = dest / TARGETED_DIR
    targeted.mkdir(parents=True, exist_ok=True)
    for scenario in target.scenarios:
        (targeted / f"{scenario.id}.yaml").write_text(scenario_to_yaml(scenario), "utf-8")
    copied_config = dest / config_path.name
    copied_config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return Workspace(root=dest, config_path=copied_config, agent_host=host)


def _read_config(config_path: Path) -> dict[str, object]:
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise FixError(f"cannot read {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise FixError(f"{config_path} is not a mapping")
    return raw


def _top_level_name(output_dir: object) -> str | None:
    path = PurePosixPath(str(output_dir))
    return path.parts[0] if path.parts and not path.is_absolute() else None


def _point_adapters_at(raw: dict[str, object], port: int) -> str:
    """Rewrite the port of every adapter URL; returns the agent's host."""
    agent = raw.get("agent")
    adapters = agent.get("adapters") if isinstance(agent, dict) else None
    hosts: list[str] = []
    for adapter in adapters.values() if isinstance(adapters, dict) else ():
        if isinstance(adapter, dict) and isinstance(adapter.get("url"), str):
            adapter["url"], host = with_port(adapter["url"], port)
            hosts.append(host)
    if not hosts:
        raise FixError(
            "the fixer starts the agent itself and reaches it by URL: no adapter in hecklr.yaml "
            "has a url"
        )
    return hosts[0]


def with_port(url: str, port: int) -> tuple[str, str]:
    """``url`` with its port replaced by ``port``, and its host."""
    parts = urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    netloc_host = f"[{host}]" if ":" in host else host
    return urlunsplit(parts._replace(netloc=f"{netloc_host}:{port}")), host


def _targeted_suite(target: FixTarget, trials: int) -> dict[str, object]:
    suite: dict[str, object] = {"scenarios": f"{TARGETED_DIR}/*.yaml", "trials": trials}
    if target.channels is not None:
        suite["channels"] = [channel.value for channel in target.channels]
    if target.personas is not None:
        suite["personas"] = target.personas
    return suite
