"""Run lifecycle step 1, Load: ``hecklr.yaml``, the world, and a suite's scenarios and personas.

Paths and globs in ``hecklr.yaml`` are relative to the file. Personas are found in two places:
the project's persona library (``personas/**/*.yaml``) and, when a suite sets ``personas``, that
glob, whose personas are then crossed with every scenario instead of the scenario's own.
"""

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from hecklr_core.errors import ConfigError, ConfigIssue
from hecklr_core.yaml_files import load_model
from hecklr_schemas import (
    Channel,
    HecklrConfig,
    Persona,
    Scenario,
    SimulationMode,
    SuiteConfig,
    World,
)

PERSONA_LIBRARY_GLOB: Final = "personas/**/*.yaml"
"""Where scenario-referenced personas are looked up, relative to ``hecklr.yaml``."""


@dataclass(frozen=True, slots=True)
class Project:
    """A loaded ``hecklr.yaml`` and its world."""

    config: HecklrConfig
    config_path: Path
    world: World

    @property
    def root(self) -> Path:
        """Directory holding ``hecklr.yaml``; every relative path resolves against it."""
        return self.config_path.parent

    @property
    def output_dir(self) -> Path:
        """Where runs and the response cache live (``.hecklr`` by default)."""
        return self.root / self.config.output_dir


@dataclass(frozen=True, slots=True)
class SuiteCell:
    """The channels and personas one scenario runs with in a suite."""

    scenario: Scenario
    persona_ids: tuple[str, ...]
    channels: tuple[Channel, ...]


@dataclass(frozen=True, slots=True)
class Suite:
    """A suite resolved against the project: every scenario, persona and channel it will run."""

    name: str
    project: Project
    settings: SuiteConfig
    cells: tuple[SuiteCell, ...]
    personas: dict[str, Persona]
    trials: int
    config_hash: str
    """SHA-256 of the resolved config, world, scenarios and personas (canonical JSON)."""

    @property
    def scenarios(self) -> dict[str, Scenario]:
        """Scenarios of the suite by id."""
        return {cell.scenario.id: cell.scenario for cell in self.cells}


def load_project(config_path: Path) -> Project:
    """Load and validate ``hecklr.yaml`` and the world it names.

    Raises:
        ConfigError: either file is unreadable or invalid; issues carry file and line.
    """
    config_path = config_path.resolve()
    config = load_model(config_path, HecklrConfig)
    world = load_model(config_path.parent / config.world, World)
    return Project(config=config, config_path=config_path, world=world)


def resolve_suite(
    project: Project,
    name: str,
    *,
    trials: int | None = None,
    channels: Sequence[Channel] | None = None,
) -> Suite:
    """Discover and cross-check the scenarios and personas of suite ``name``.

    ``trials`` and ``channels`` override the suite's own settings (``--trials``, ``--channels``).

    Raises:
        ConfigError: unknown suite, invalid files, duplicate ids, dangling references, or a suite
            that selects no trial.
    """
    settings = project.config.suites.get(name)
    if settings is None:
        known = ", ".join(sorted(project.config.suites))
        raise ConfigError([_config_issue(project, f"unknown suite {name!r}; defined: {known}")])
    if trials is not None and trials < 1:
        raise ValueError(f"trials must be >= 1, got {trials}")
    wanted_channels = list(channels) if channels is not None else settings.channels
    _check_adapters(project, wanted_channels)

    scenario_files = _glob(project.root, settings.scenarios)
    scenarios = _load_unique(scenario_files, Scenario)
    library = _load_unique(_glob(project.root, PERSONA_LIBRARY_GLOB), Persona)
    crossed: dict[str, tuple[Persona, Path]] | None = None
    if settings.personas is not None:
        crossed = _load_unique(_glob(project.root, settings.personas), Persona)
        if not crossed:
            raise ConfigError(
                [_config_issue(project, f"suite {name!r}: personas glob matches no file")]
            )

    issues: list[ConfigIssue] = []
    cells: list[SuiteCell] = []
    used: dict[str, Persona] = {}
    for scenario, path in sorted(scenarios.values(), key=lambda item: item[0].id):
        if settings.tags and not set(settings.tags) & set(scenario.tags):
            continue
        cell_personas = _personas_for(scenario, path, project.world, library, crossed, issues)
        cell_channels = _channels_for(scenario, project, wanted_channels)
        if not cell_personas or not cell_channels:
            continue
        for persona in cell_personas:
            if Channel.VOICE in cell_channels and persona.voice is None:
                issues.append(
                    ConfigIssue(
                        path,
                        None,
                        f"scenario {scenario.id!r} runs on voice but persona {persona.id!r} "
                        "has no voice profile",
                    )
                )
            used[persona.id] = persona
        cells.append(
            SuiteCell(
                scenario=scenario,
                persona_ids=tuple(persona.id for persona in cell_personas),
                channels=cell_channels,
            )
        )
    if issues:
        raise ConfigError(issues)
    if not cells:
        raise ConfigError(
            [_config_issue(project, f"suite {name!r} selects no scenario (check globs and tags)")]
        )
    suite_trials = trials if trials is not None else settings.trials
    return Suite(
        name=name,
        project=project,
        settings=settings,
        cells=tuple(cells),
        personas=dict(sorted(used.items())),
        trials=suite_trials,
        config_hash=config_hash(
            project.config,
            project.world,
            [cell.scenario for cell in cells],
            used.values(),
            extra={"suite": name, "trials": suite_trials, "channels": wanted_channels},
        ),
    )


def config_hash(
    config: HecklrConfig,
    world: World,
    scenarios: Iterable[Scenario],
    personas: Iterable[Persona],
    *,
    extra: dict[str, object] | None = None,
) -> str:
    """SHA-256 over canonical JSON of everything that defines what a run tests."""
    document = {
        "config": config.model_dump(mode="json"),
        "world": world.model_dump(mode="json"),
        "scenarios": sorted(
            (scenario.model_dump(mode="json") for scenario in scenarios), key=_by_id
        ),
        "personas": sorted((persona.model_dump(mode="json") for persona in personas), key=_by_id),
        "extra": extra or {},
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _by_id(dumped: dict[str, object]) -> str:
    return str(dumped["id"])


def _config_issue(project: Project, message: str) -> ConfigIssue:
    return ConfigIssue(project.config_path, None, message)


def _check_adapters(project: Project, channels: Sequence[Channel] | None) -> None:
    if channels is None:
        return
    configured = set(project.config.agent.adapters.channels())
    missing = [channel.value for channel in channels if channel not in configured]
    if missing:
        raise ConfigError(
            [
                _config_issue(
                    project,
                    f"channels {missing} are requested but agent.adapters configures none for them",
                )
            ]
        )


def _glob(root: Path, pattern: str) -> list[Path]:
    return sorted(path for path in root.glob(pattern) if path.is_file())


def _load_unique[M: (Scenario, Persona)](
    paths: Sequence[Path], model: type[M]
) -> dict[str, tuple[M, Path]]:
    loaded: dict[str, tuple[M, Path]] = {}
    issues: list[ConfigIssue] = []
    for path in paths:
        try:
            item = load_model(path, model)
        except ConfigError as exc:
            issues.extend(exc.issues)
            continue
        if item.id in loaded:
            other = loaded[item.id][1]
            issues.append(ConfigIssue(path, None, f"id {item.id!r} is also defined in {other}"))
            continue
        loaded[item.id] = (item, path)
    if issues:
        raise ConfigError(issues)
    return loaded


def _personas_for(
    scenario: Scenario,
    path: Path,
    world: World,
    library: dict[str, tuple[Persona, Path]],
    crossed: dict[str, tuple[Persona, Path]] | None,
    issues: list[ConfigIssue],
) -> list[Persona]:
    if scenario.world != world.id:
        issues.append(
            ConfigIssue(
                path,
                None,
                f"scenario {scenario.id!r} needs world {scenario.world!r}, "
                f"but the project's world is {world.id!r}",
            )
        )
    if crossed is not None:
        personas = [persona for persona, _ in sorted(crossed.values(), key=lambda p: p[0].id)]
    else:
        personas = []
        for persona_id in scenario.persona_ids:
            entry = library.get(persona_id)
            if entry is None:
                issues.append(
                    ConfigIssue(
                        path,
                        None,
                        f"scenario {scenario.id!r} references unknown persona {persona_id!r} "
                        f"(looked in {PERSONA_LIBRARY_GLOB})",
                    )
                )
            else:
                personas.append(entry[0])
    if scenario.mode is SimulationMode.ADVERSARIAL:
        for persona in personas:
            if persona.adversarial is None:
                issues.append(
                    ConfigIssue(
                        path,
                        None,
                        f"adversarial scenario {scenario.id!r} is crossed with persona "
                        f"{persona.id!r}, which has no adversarial strategy",
                    )
                )
    return personas


def _channels_for(
    scenario: Scenario, project: Project, wanted: Sequence[Channel] | None
) -> tuple[Channel, ...]:
    configured = set(project.config.agent.adapters.channels())
    return tuple(
        channel
        for channel in scenario.channels
        if channel in configured and (wanted is None or channel in wanted)
    )
