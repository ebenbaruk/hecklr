"""The benchmark's authored files: ``bench.yaml`` (the manifest) and ``stacks/*.yaml``.

A stack is one way of building the agent under test (transport, VAD, ASR, brain, TTS), started
as a local process from a command line. Its command, environment, readiness check and adapter
entry may name placeholders that the runner fills per run: ``{python}`` (this interpreter),
``{repo}`` (the directory holding ``bench/``), ``{port}``, ``{port1}``, ``{port2}`` (free
127.0.0.1 ports), ``{seed}`` (the benchmark seed) and ``{agent_budget_usd}``.
"""

import re
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import Field, JsonValue

from hecklr_core import ConfigError, ConfigIssue, load_model
from hecklr_schemas import FrozenModel, StatsConfig
from hecklr_schemas.common import Identifier, ModelName
from hecklr_schemas.config import EnvVarName

MANIFEST: Final = "bench.yaml"
STACKS_DIR: Final = "stacks"
RESULTS_DIR: Final = "results"
Track = Literal["chat", "voice", "calibration"]
PLACEHOLDER: Final = re.compile(r"\{(\w+)\}")
PORTS: Final = ("port", "port1", "port2")


class TrackConfig(FrozenModel):
    """The scenarios a track runs and how often."""

    scenarios: str
    """Glob of scenario files, relative to the suite directory."""
    trials: Annotated[int, Field(ge=1)]
    asr: ModelName | None = None
    """How Hecklr hears the agent (voice tracks); the same ears for every stack."""


class BenchModels(FrozenModel):
    """The models Hecklr itself calls: guided customers and judges."""

    simulator: ModelName
    judge: ModelName


class Manifest(FrozenModel):
    """``bench.yaml``: what every stack is measured on."""

    version: str
    """Benchmark version; results of different versions are never compared."""
    seed: int
    suite: str
    """The suite directory (scenarios, personas, world), relative to ``bench.yaml``."""
    world: str
    """The world file, relative to the suite directory."""
    models: BenchModels
    stats: StatsConfig = StatsConfig()
    tracks: dict[Track, TrackConfig]


class Ready(FrozenModel):
    """How the runner knows the agent accepts calls: an HTTP 200, or a line in its output."""

    http: str | None = None
    line: str | None = None
    """A regular expression searched in the agent's combined stdout and stderr."""
    timeout_s: Annotated[float, Field(gt=0)] = 300.0


class StackSpec(FrozenModel):
    """One stack: how to start it, reach it and what it is made of."""

    id: Identifier
    name: str
    track: Track
    description: str
    why: str
    """What this row tells a reader that the other rows do not."""
    components: dict[str, str]
    """Transport, VAD, turn detection, ASR, brain, TTS: what the leaderboard shows."""
    command: list[str]
    env: dict[str, str] = Field(default_factory=dict)
    """Set for the agent process and, during the run, for Hecklr (e.g. LiveKit credentials)."""
    requires_env: list[EnvVarName] = Field(default_factory=list)
    """Variables that must already be set (API keys); never written to results."""
    ready: Ready
    adapter: dict[str, JsonValue]
    """The ``agent.adapters.<channel>`` entry of ``hecklr.yaml``."""
    spend_url: str | None = None
    """``GET`` returns JSON with ``spent_usd``: the agent's own model and speech spend so far."""
    budget_usd: Annotated[Decimal, Field(ge=0)]
    """Hard cap on Hecklr's own spend (simulator, judges) for one run of this stack."""
    agent_budget_usd: Annotated[Decimal, Field(ge=0)] = Decimal(0)
    """Cap the agent enforces on its own spend (passed as ``{agent_budget_usd}``)."""
    reference: bool = False
    """A calibration row: its timing is known, so it measures the harness, not an agent."""

    @property
    def max_usd(self) -> Decimal:
        """The most one run of this stack can cost."""
        return self.budget_usd + self.agent_budget_usd


def load_manifest(bench_dir: Path) -> Manifest:
    """``bench.yaml`` of ``bench_dir``.

    Raises:
        ConfigError: missing or invalid.
    """
    path = bench_dir / MANIFEST
    if not path.is_file():
        raise ConfigError([ConfigIssue(path, None, "no benchmark manifest here (--bench-dir)")])
    return load_model(path, Manifest)


def load_stacks(bench_dir: Path) -> dict[str, StackSpec]:
    """Every stack of ``bench_dir/stacks``, by id.

    Raises:
        ConfigError: an invalid file, or a file whose name is not its stack id.
    """
    stacks: dict[str, StackSpec] = {}
    for path in sorted((bench_dir / STACKS_DIR).glob("*.yaml")):
        stack = load_model(path, StackSpec)
        if path.stem != stack.id:
            raise ConfigError([ConfigIssue(path, None, f"file name must be {stack.id}.yaml")])
        stacks[stack.id] = stack
    return stacks


def select_stacks(stacks: Mapping[str, StackSpec], ids: list[str]) -> list[StackSpec]:
    """The stacks ``ids`` names, in that order.

    Raises:
        ConfigError: an unknown id.
    """
    unknown = [i for i in ids if i not in stacks]
    if unknown:
        known = ", ".join(stacks) or "none"
        raise ConfigError(
            [ConfigIssue(Path(STACKS_DIR), None, f"unknown stack {unknown}; known: {known}")]
        )
    return [stacks[i] for i in ids]


def fill(template: str, values: Mapping[str, str]) -> str:
    """``template`` with every ``{name}`` replaced from ``values``.

    Raises:
        KeyError: a placeholder ``values`` does not define (a typo in a stack file).
    """
    return PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


def fill_json(value: JsonValue, values: Mapping[str, str]) -> JsonValue:
    """``fill`` applied to every string inside a JSON value."""
    if isinstance(value, str):
        return fill(value, values)
    if isinstance(value, list):
        return [fill_json(item, values) for item in value]
    if isinstance(value, dict):
        return {key: fill_json(item, values) for key, item in value.items()}
    return value
