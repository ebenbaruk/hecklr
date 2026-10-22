"""``hecklr.yaml`` as the CLI reads it: the frozen schema plus the ``websocket`` voice adapter.

RFC 0010a (accepted) adds the ``websocket`` voice adapter to
``hecklr_schemas.VoiceAdapterConfig``. Until the schema carries it, these subclasses widen
``agent.adapters.voice`` by that one member, so ``hecklr.yaml`` can select the transport most
production voice stacks speak (raw PCM, Twilio Media Streams, Pipecat protobuf). Everything else
is the schema's own validation; instances are still ``HecklrConfig``. When the schema gains the
member, this module goes and ``load_run_project`` validates ``HecklrConfig`` directly.
"""

from pathlib import Path
from typing import Annotated

from pydantic import Field

from hecklr_adapters import WebSocketVoiceAdapterConfig
from hecklr_core import Project
from hecklr_core.yaml_files import load_model
from hecklr_schemas import (
    AgentAdapters,
    AgentConfig,
    HecklrConfig,
    LiveKitAdapterConfig,
    PipecatAdapterConfig,
    SipAdapterConfig,
    WebRTCAdapterConfig,
    World,
)
from hecklr_world import load_world

VoiceAdapter = Annotated[
    PipecatAdapterConfig
    | LiveKitAdapterConfig
    | WebRTCAdapterConfig
    | SipAdapterConfig
    | WebSocketVoiceAdapterConfig,
    Field(discriminator="type"),
]
"""The schema's voice adapters plus ``websocket`` (RFC 0010a)."""


class ProjectAdapters(AgentAdapters):
    """``agent.adapters`` with the ``websocket`` voice adapter."""

    # Wider than the schema's field by exactly the RFC 0010a member; mypy sees the override.
    voice: VoiceAdapter | None = None  # type: ignore[assignment]


class ProjectAgent(AgentConfig):
    """``agent`` with ``ProjectAdapters``."""

    adapters: ProjectAdapters


class ProjectConfig(HecklrConfig):
    """Root of ``hecklr.yaml`` with ``ProjectAgent``."""

    agent: ProjectAgent


def load_project_config(config_path: Path) -> Project:
    """Load and validate ``hecklr.yaml`` and its world, with the world's seed files inlined.

    Raises:
        hecklr_core.ConfigError: ``hecklr.yaml`` is unreadable or invalid (file and line).
        hecklr_world.WorldConfigError: the world or its seed data cannot be loaded.
    """
    config_path = config_path.resolve()
    config = load_model(config_path, ProjectConfig)
    world_path = config_path.parent / config.world
    # Validated as a file first for file-and-line errors; load_world then inlines the seed files.
    load_model(world_path, World)
    return Project(config=config, config_path=config_path, world=load_world(world_path))
