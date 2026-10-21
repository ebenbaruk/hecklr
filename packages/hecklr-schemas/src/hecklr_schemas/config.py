"""``hecklr.yaml``: the one configuration file for a project.

Secrets never live here: fields ending in ``_env`` name the environment variable that holds them.
"""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, HttpUrl, JsonValue, model_validator

from hecklr_schemas.common import (
    Budget,
    Channel,
    FrozenModel,
    Identifier,
    ModelName,
    SchemaVersion,
)
from hecklr_schemas.events import EmailAddress
from hecklr_schemas.llm import ModelPrice
from hecklr_schemas.world import EntryPoint

EnvVarName = Annotated[str, Field(pattern=r"^[A-Z_][A-Z0-9_]*$")]
PositiveSeconds = Annotated[float, Field(gt=0)]
Port = Annotated[int, Field(ge=1, le=65535)]

# --- adapters -------------------------------------------------------------------------------


class HttpAdapterConfig(FrozenModel):
    """Templated JSON request/response chat endpoint, optionally streamed."""

    type: Literal["http"]
    url: HttpUrl
    stream: Literal["none", "sse", "chunked"] = "none"
    headers: dict[str, str] = Field(default_factory=dict)
    request_template: dict[str, JsonValue] | None = None
    """JSON body with ``{{message}}``, ``{{session_id}}`` and ``{{trial_id}}`` placeholders."""
    response_path: str | None = None
    """Dotted path to the reply text in the response JSON; the whole body if ``None``."""
    timeout_s: PositiveSeconds = 60.0


class OpenAICompatibleAdapterConfig(FrozenModel):
    """An agent behind any ``/v1/chat/completions`` endpoint."""

    type: Literal["openai-compatible"]
    url: HttpUrl
    """Base URL, e.g. ``http://localhost:8000/v1``."""
    model: str | None = None
    api_key_env: EnvVarName | None = None
    stream: bool = True
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_s: PositiveSeconds = 60.0


class PythonAdapterConfig(FrozenModel):
    """An in-process agent callable decorated with ``@hecklr.agent``."""

    type: Literal["python"]
    entrypoint: EntryPoint


class WebSocketAdapterConfig(FrozenModel):
    """A chat agent over WebSocket with a configurable message schema."""

    type: Literal["websocket"]
    url: Annotated[str, Field(pattern=r"^wss?://")]
    send_template: dict[str, JsonValue] | None = None
    receive_path: str | None = None
    final_path: str | None = None
    """Dotted path to a boolean marking the agent's last message of a turn."""
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_s: PositiveSeconds = 60.0


class WebhookAdapterConfig(FrozenModel):
    """Hecklr posts user messages to ``url`` and exposes an endpoint the agent replies to."""

    type: Literal["webhook"]
    url: HttpUrl
    listen_host: str = "127.0.0.1"
    listen_port: Port = 8787
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_s: PositiveSeconds = 120.0


class PipecatAdapterConfig(FrozenModel):
    """Pipecat SmallWebRTC offer/answer endpoint."""

    type: Literal["pipecat"]
    url: HttpUrl


class LiveKitAdapterConfig(FrozenModel):
    """Joins a LiveKit room as a participant."""

    type: Literal["livekit"]
    url: Annotated[str, Field(pattern=r"^(wss?|https?)://")]
    room_prefix: str = "hecklr"
    """Each trial gets its own room ``<prefix>-<trial_id>``."""
    api_key_env: EnvVarName = "LIVEKIT_API_KEY"
    api_secret_env: EnvVarName = "LIVEKIT_API_SECRET"  # noqa: S105 -- names the variable, holds no secret


class WebRTCAdapterConfig(FrozenModel):
    """Generic WHIP-style WebRTC offer/answer."""

    type: Literal["webrtc"]
    url: HttpUrl
    headers: dict[str, str] = Field(default_factory=dict)


class SipAdapterConfig(FrozenModel):
    """Calls a SIP URI through a local user agent (tested against Asterisk)."""

    type: Literal["sip"]
    uri: Annotated[str, Field(pattern=r"^sips?:")]
    proxy: str | None = None
    username_env: EnvVarName | None = None
    password_env: EnvVarName | None = None


class SmtpImapAdapterConfig(FrozenModel):
    """Email agent reached over SMTP; replies collected over IMAP. ``smtp`` is an alias."""

    type: Literal["smtp-imap", "smtp"]
    inbox: EmailAddress
    """The agent's address, e.g. ``support@acme.test``."""
    from_domain: str = "customers.hecklr.test"
    smtp_host: str = "localhost"
    smtp_port: Port = 1025
    imap_host: str = "localhost"
    imap_port: Port = 1143
    username_env: EnvVarName | None = None
    password_env: EnvVarName | None = None
    reply_timeout_s: PositiveSeconds = 120.0


ChatAdapterConfig = Annotated[
    HttpAdapterConfig
    | OpenAICompatibleAdapterConfig
    | PythonAdapterConfig
    | WebSocketAdapterConfig
    | WebhookAdapterConfig,
    Field(discriminator="type"),
]
VoiceAdapterConfig = Annotated[
    PipecatAdapterConfig | LiveKitAdapterConfig | WebRTCAdapterConfig | SipAdapterConfig,
    Field(discriminator="type"),
]
EmailAdapterConfig = Annotated[
    SmtpImapAdapterConfig | WebhookAdapterConfig,
    Field(discriminator="type"),
]


class AgentAdapters(FrozenModel):
    """How to reach the agent on each channel it supports."""

    chat: ChatAdapterConfig | None = None
    voice: VoiceAdapterConfig | None = None
    email: EmailAdapterConfig | None = None

    def channels(self) -> list[Channel]:
        """Channels that have an adapter configured."""
        return [channel for channel in Channel if getattr(self, channel.value) is not None]


class AgentConfig(FrozenModel):
    """The agent under test."""

    name: Identifier
    adapters: AgentAdapters
    editable: list[str] = Field(default_factory=list)
    """Files the fixer may change (system prompt, tool descriptions); nothing else."""
    tool_mode: Literal["sandbox", "observed"] = "sandbox"
    """``sandbox``: the agent's tools call the world sandbox. ``observed``: the agent keeps its own
    backend and tool calls are read from the OpenTelemetry GenAI spans it emits, joined to trials
    by the ``X-Hecklr-Trial-Id`` header."""

    @model_validator(mode="after")
    def _has_an_adapter(self) -> Self:
        if not self.adapters.channels():
            raise ValueError("agent.adapters must configure at least one channel")
        return self


# --- suites, models, stats ------------------------------------------------------------------


class SuiteConfig(FrozenModel):
    """A named selection of scenarios and personas, e.g. ``ci`` or ``nightly``."""

    scenarios: str = "scenarios/**/*.yaml"
    """Glob of scenario files, relative to ``hecklr.yaml``."""
    personas: str | None = None
    """Glob of persona files to cross every scenario with; scenario personas if ``None``."""
    tags: list[Identifier] = Field(default_factory=list)
    """Keep scenarios carrying any of these tags; all scenarios if empty."""
    trials: Annotated[int, Field(ge=1)] = 3
    channels: list[Channel] | None = None
    """Restrict to these channels; each scenario's own channels if ``None``."""
    adaptive: bool = False


class ProviderSettings(FrozenModel):
    """Connection settings for one model provider prefix."""

    base_url: HttpUrl | None = None
    api_key_env: EnvVarName | None = None


class ModelsConfig(FrozenModel):
    """Models Hecklr itself uses, as ``provider/model`` strings."""

    simulator: ModelName
    judge: ModelName
    asr: ModelName | None = None
    tts: ModelName | None = None
    providers: dict[Identifier, ProviderSettings] = Field(default_factory=dict)
    """Per-prefix overrides, e.g. ``openai-compatible: {base_url: http://gpu:8000/v1}``."""
    prices: dict[ModelName, ModelPrice] = Field(default_factory=dict)
    """Prices for models missing from Hecklr's table, or overrides of it. A paid model without a
    known price is refused rather than counted as free; declare a self-hosted model at 0."""


class StatsConfig(FrozenModel):
    """Statistical decision settings (see the spec's verdict rule)."""

    alpha: Annotated[float, Field(gt=0.0, lt=0.5)] = 0.05
    min_effect: Annotated[float, Field(ge=0.0, lt=1.0)] = 0.03
    """Minimum pass-rate change (absolute, 0.03 = 3 points) that counts as a real change."""
    bootstrap_resamples: Annotated[int, Field(ge=100)] = 10_000
    seed: int | None = None
    """Bootstrap and permutation seed; the run seed when ``None``."""


class BaselineConfig(FrozenModel):
    """Where the baseline run for ``hecklr gate`` is stored."""

    store: Literal["local", "github-artifacts", "s3", "gcs"] = "local"
    branch: str = "main"
    uri: str | None = None
    """Bucket URI for ``s3``/``gcs``, directory for ``local``."""


class GateConfig(FrozenModel):
    """How ``hecklr gate`` maps verdicts to exit codes."""

    on_inconclusive: Literal["fail", "pass"] = "fail"
    """``fail`` exits 2 on an inconclusive verdict; ``pass`` exits 0."""


class HecklrConfig(FrozenModel):
    """Root of ``hecklr.yaml``."""

    version: SchemaVersion
    agent: AgentConfig
    world: str
    """Path to the world file, relative to ``hecklr.yaml``."""
    suites: Annotated[dict[Identifier, SuiteConfig], Field(min_length=1)]
    models: ModelsConfig
    stats: StatsConfig = StatsConfig()
    budget: Budget = Budget(usd=Decimal(25))
    baseline: BaselineConfig = BaselineConfig()
    gate: GateConfig = GateConfig()
    seed: int = 0
    """Root seed; every trial seed is derived from it."""
    output_dir: str = ".hecklr"
