"""The starter project ``hecklr init`` writes: config, a world, 5 scenarios, 3 personas.

The world, scenarios and personas are package files under ``hecklr/starter``; ``hecklr.yaml`` is
rendered from the chosen agent adapter and models. With no agent to point at, the project gets
``hecklr_agent.py``, a small in-process agent, so the first ``hecklr run`` works right away.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Final, Literal

from hecklr_schemas.common import ModelName

CONFIG_FILE: Final = "hecklr.yaml"
STARTER_AGENT_MODULE: Final = "hecklr_agent"
STARTER_AGENT_ENTRYPOINT: Final = f"{STARTER_AGENT_MODULE}:reply"
PREFERRED_OLLAMA_MODELS: Final = ("qwen3:4b-instruct", "qwen3:8b")
DEFAULT_OLLAMA_MODEL: Final = "ollama/qwen3:4b-instruct"

_STARTER_DIRS: Final = ("worlds", "scenarios", "personas")

AdapterType = Literal["python", "http", "openai-compatible"]


@dataclass(frozen=True, slots=True)
class AgentChoice:
    """How the project reaches its agent."""

    adapter: AdapterType
    url: str | None = None
    """Endpoint for ``http`` and ``openai-compatible``; ``python`` uses the starter agent."""
    model: str | None = None
    """Model name an ``openai-compatible`` agent serves, when the server needs one."""


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """The simulator and judge models, and why they were chosen."""

    simulator: ModelName
    judge: ModelName
    reason: str


def choose_models(env: Mapping[str, str], ollama: list[str] | None) -> ModelChoice:
    """Pick models that work on this machine: a local Ollama first, then any API key present."""
    if ollama:
        local = next((m for m in PREFERRED_OLLAMA_MODELS if m in ollama), ollama[0])
        model = f"ollama/{local}"
        return ModelChoice(model, model, "local Ollama is running (free)")
    if env.get("ANTHROPIC_API_KEY"):
        return ModelChoice(
            "anthropic/claude-haiku-5-5", "anthropic/claude-sonnet-5-5", "ANTHROPIC_API_KEY is set"
        )
    if env.get("OPENAI_API_KEY"):
        return ModelChoice("openai/gpt-4.1-mini", "openai/gpt-4.1", "OPENAI_API_KEY is set")
    return ModelChoice(
        DEFAULT_OLLAMA_MODEL,
        DEFAULT_OLLAMA_MODEL,
        "no Ollama or API key found: start `ollama serve` and `ollama pull qwen3:4b-instruct`",
    )


def existing_files(root: Path, choice: AgentChoice) -> list[Path]:
    """Files ``write_project`` would overwrite."""
    return [path for path in planned_files(root, choice) if path.exists()]


def planned_files(root: Path, choice: AgentChoice) -> list[Path]:
    """Every file ``write_project`` writes, in order."""
    paths = [root / CONFIG_FILE]
    for directory in _STARTER_DIRS:
        paths.extend(root / directory / entry.name for entry in _starter_files(directory))
    if choice.adapter == "python":
        paths.append(root / f"{STARTER_AGENT_MODULE}.py")
    return paths


def write_project(
    root: Path, agent_name: str, choice: AgentChoice, models: ModelChoice
) -> list[Path]:
    """Write the starter project under ``root`` and return the files written."""
    written = []
    root.mkdir(parents=True, exist_ok=True)
    config = root / CONFIG_FILE
    config.write_text(render_config(agent_name, choice, models), encoding="utf-8")
    written.append(config)
    for directory in _STARTER_DIRS:
        target_dir = root / directory
        target_dir.mkdir(exist_ok=True)
        for entry in _starter_files(directory):
            target = target_dir / entry.name
            target.write_text(entry.read_text(encoding="utf-8"), encoding="utf-8")
            written.append(target)
    if choice.adapter == "python":
        agent = root / f"{STARTER_AGENT_MODULE}.py"
        agent.write_text(_starter().joinpath("agent.py").read_text("utf-8"), encoding="utf-8")
        written.append(agent)
    return written


def render_config(agent_name: str, choice: AgentChoice, models: ModelChoice) -> str:
    """``hecklr.yaml`` for the starter project, with comments where users edit first."""
    return f"""\
# Hecklr configuration. Reference: docs/SPEC.md, "Configuration".
version: 1
agent:
  name: {agent_name}
  adapters:
    chat: {_adapter_yaml(choice)}
world: worlds/acme-starter.yaml
suites:
  ci: {{ scenarios: "scenarios/**/*.yaml", tags: [core], trials: 3, channels: [chat] }}
models:
  # {models.reason}
  simulator: {models.simulator}
  judge: {models.judge}
stats: {{ alpha: 0.05, min_effect: 0.03 }}
# A hard cap on model spend; local Ollama models are free.
budget: {{ usd: 5, concurrency: 8 }}
"""


def _adapter_yaml(choice: AgentChoice) -> str:
    # JSON is valid YAML flow style, and it quotes URLs and entry points safely.
    if choice.adapter == "python":
        return json.dumps({"type": "python", "entrypoint": STARTER_AGENT_ENTRYPOINT})
    adapter: dict[str, str] = {"type": choice.adapter, "url": choice.url or ""}
    if choice.adapter == "openai-compatible" and choice.model:
        adapter["model"] = choice.model
    return json.dumps(adapter)


def _starter() -> Traversable:
    return resources.files("hecklr").joinpath("starter")


def _starter_files(directory: str) -> list[Traversable]:
    return sorted(
        (
            entry
            for entry in _starter().joinpath(directory).iterdir()
            if entry.name.endswith(".yaml")
        ),
        key=lambda entry: entry.name,
    )
