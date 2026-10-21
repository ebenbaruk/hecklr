"""Settings and the agent's editable files, optionally overlaid by a fault variant.

The agent's behaviour lives in two files the fixer may edit (``hecklr.yaml`` ``editable``): the
system prompt and the tool catalog. A fault variant (``examples/acme-faults/F<n>/``) is an overlay
directory holding replacements for some of those files and nothing else, so launching the agent
with ``--fault F3`` changes exactly what the variant's README says and no code path.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final, Literal, cast

import yaml
from pydantic import JsonValue

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
"""``examples/acme-maintenance``: where ``hecklr.yaml`` and the clean agent files live."""

FAULTS_ROOT: Final = PROJECT_ROOT.parent / "acme-faults"
SYSTEM_PROMPT: Final = Path("prompts/system.md")
TOOL_CATALOG: Final = Path("tools/descriptions.yaml")
WORLD_FILE: Final = Path("worlds/acme-maintenance.yaml")
EDITABLE: Final = (SYSTEM_PROMPT, TOOL_CATALOG)
OVERLAY_DOCS: Final = frozenset({"README.md"})
"""Files an overlay may hold besides the editable ones."""

TODAY: Final = date(2026, 10, 21)
"""The world's "today"; fixed so a trial behaves the same on any calendar day."""

DEFAULT_MODEL: Final = "ollama/qwen3:8b"
Brain = Literal["llm", "rules"]
_FAULT_ID = re.compile(r"^F\d+$")


class AgentConfigError(ValueError):
    """The agent cannot start: a bad setting, a missing file or an overlay that changes code."""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool as the agent's model is told about it."""

    name: str
    description: str
    parameters: dict[str, JsonValue]
    """JSON Schema (``type: object``) of the tool's arguments."""

    def parameter_description(self, parameter: str) -> str:
        """The description of one parameter, or ``""``."""
        properties = self.parameters.get("properties")
        spec = properties.get(parameter) if isinstance(properties, dict) else None
        text = spec.get("description") if isinstance(spec, dict) else None
        return text if isinstance(text, str) else ""


@dataclass(frozen=True, slots=True)
class AgentFiles:
    """The editable files the agent runs from."""

    system_prompt: str
    tools: dict[str, ToolSpec]
    variant: str
    """``clean`` or the overlay directory's name (e.g. ``F3``)."""

    def tool(self, name: str) -> ToolSpec:
        """The catalog entry of ``name``."""
        try:
            return self.tools[name]
        except KeyError:
            raise AgentConfigError(f"the tool catalog has no tool {name!r}") from None


def fault_overlay(fault_id: str, faults_root: Path = FAULTS_ROOT) -> Path:
    """The overlay directory of fault variant ``fault_id`` (e.g. ``F3``)."""
    if not _FAULT_ID.match(fault_id):
        raise AgentConfigError(f"fault ids look like F1..F12, got {fault_id!r}")
    overlay = faults_root / fault_id
    if not overlay.is_dir():
        raise AgentConfigError(f"no fault variant {fault_id!r} in {faults_root}")
    return overlay


def load_agent_files(root: Path = PROJECT_ROOT, overlay: Path | None = None) -> AgentFiles:
    """Read the system prompt and tool catalog, taking each from ``overlay`` when it has one.

    Raises:
        AgentConfigError: a file is missing or malformed, or the overlay holds any other file.
    """
    if overlay is not None:
        _check_overlay(overlay)

    def resolve(relative: Path) -> Path:
        candidate = overlay / relative if overlay is not None else None
        return candidate if candidate is not None and candidate.is_file() else root / relative

    prompt_path, catalog_path = resolve(SYSTEM_PROMPT), resolve(TOOL_CATALOG)
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
        catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AgentConfigError(f"cannot read the agent files: {exc}") from exc
    return AgentFiles(
        system_prompt=prompt,
        tools=_parse_catalog(catalog, catalog_path),
        variant="clean" if overlay is None else overlay.name,
    )


def _check_overlay(overlay: Path) -> None:
    if not overlay.is_dir():
        raise AgentConfigError(f"overlay {overlay} is not a directory")
    allowed = {str(path) for path in EDITABLE} | OVERLAY_DOCS
    files = {str(path.relative_to(overlay)) for path in overlay.rglob("*") if path.is_file()}
    if unexpected := sorted(files - allowed):
        raise AgentConfigError(f"overlay {overlay} may only replace editable files: {unexpected}")


def _parse_catalog(document: object, path: Path) -> dict[str, ToolSpec]:
    tools = document.get("tools") if isinstance(document, dict) else None
    if not isinstance(tools, dict) or not tools:
        raise AgentConfigError(f"{path}: expected a non-empty 'tools' mapping")
    specs: dict[str, ToolSpec] = {}
    for name, entry in tools.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("description"), str):
            raise AgentConfigError(f"{path}: tool {name!r} needs a description")
        parameters = entry.get("parameters", {"type": "object", "properties": {}})
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise AgentConfigError(f"{path}: tool {name!r} parameters must be an object schema")
        specs[str(name)] = ToolSpec(
            name=str(name),
            description=" ".join(entry["description"].split()),
            parameters=cast(dict[str, JsonValue], parameters),
        )
    return specs


@dataclass(frozen=True, slots=True)
class AgentSettings:
    """How the agent runs; every field has an ``ACME_*`` environment variable."""

    brain: Brain = "llm"
    model: str = DEFAULT_MODEL
    """``provider/model`` for the LLM brain, as ``hecklr_llm`` names models."""
    fault: str | None = None
    """Fault variant to overlay, e.g. ``F3``; ``None`` runs the clean agent."""
    overlay: Path | None = None
    """An explicit overlay directory (takes precedence over ``fault``)."""
    world_base_url: str | None = None
    """Root URL of a running Hecklr world sandbox; the trial's tools are at ``/t/<trial id>``."""
    today: date = TODAY
    seed: int = 0
    """Base seed of the LLM brain's requests."""
    temperature: float = 0.0
    max_steps: int = 8
    """Tool calls plus the reply the LLM brain may take for one customer message."""
    tool_timeout_s: float = 10.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AgentSettings":
        """Read settings from the environment.

        Variables: ``ACME_BRAIN``, ``ACME_MODEL``, ``ACME_FAULT``, ``ACME_OVERLAY``,
        ``ACME_WORLD_BASE_URL``, ``ACME_TODAY``, ``ACME_SEED`` and ``ACME_TOOL_TIMEOUT_S``.
        """
        source = os.environ if env is None else env
        brain = source.get("ACME_BRAIN", "llm")
        if brain not in ("llm", "rules"):
            raise AgentConfigError(f"ACME_BRAIN must be 'llm' or 'rules', got {brain!r}")
        overlay = source.get("ACME_OVERLAY")
        try:
            return cls(
                brain=cast(Brain, brain),
                model=source.get("ACME_MODEL", DEFAULT_MODEL),
                fault=source.get("ACME_FAULT") or None,
                overlay=Path(overlay) if overlay else None,
                world_base_url=source.get("ACME_WORLD_BASE_URL") or None,
                today=date.fromisoformat(source.get("ACME_TODAY", TODAY.isoformat())),
                seed=int(source.get("ACME_SEED", "0")),
                tool_timeout_s=float(source.get("ACME_TOOL_TIMEOUT_S", "10")),
            )
        except ValueError as exc:
            raise AgentConfigError(f"invalid ACME_* setting: {exc}") from exc

    def overlay_dir(self) -> Path | None:
        """The overlay to load, from ``overlay`` or ``fault``."""
        if self.overlay is not None:
            return self.overlay
        return fault_overlay(self.fault) if self.fault is not None else None
