"""Scenarios: what the customer wants, what they know, and what success looks like."""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from hecklr_schemas.common import Channel, FrozenModel, Identifier, ModelName, SchemaVersion
from hecklr_schemas.ids import ClusterIdField
from hecklr_schemas.world import ToolFault

Speaker = Literal["user", "agent"]

# --- success criteria -----------------------------------------------------------------------
# One model per built-in grader family, discriminated on ``type`` exactly as written in YAML.


class ToolCalledCriterion(FrozenModel):
    """At least ``min_calls`` calls to ``tool`` whose arguments contain ``args``."""

    type: Literal["tool_called"]
    tool: str
    args: dict[str, JsonValue] = Field(default_factory=dict)
    """Subset match: every listed argument must be present with an equal value."""
    min_calls: Annotated[int, Field(ge=1)] = 1


class ToolNotCalledCriterion(FrozenModel):
    """No call to ``tool`` (optionally: no call whose arguments contain ``args``)."""

    type: Literal["tool_not_called"]
    tool: str
    args: dict[str, JsonValue] = Field(default_factory=dict)


class ArgsMatchCriterion(FrozenModel):
    """Every call to ``tool`` has these exact argument values and regex-matching strings.

    Differs from ``tool_called``: that asks whether one good call exists, this asks whether all
    calls are well-formed (e.g. dates always ISO 8601).
    """

    type: Literal["args_match"]
    tool: str
    args: dict[str, JsonValue] = Field(default_factory=dict)
    patterns: dict[str, str] = Field(default_factory=dict)
    """Argument name -> regex that the argument's string form must fully match."""

    @model_validator(mode="after")
    def _has_a_constraint(self) -> Self:
        if not self.args and not self.patterns:
            raise ValueError("args_match needs at least one of args or patterns")
        return self


class CallOrderCriterion(FrozenModel):
    """Tools are called in this order (as a subsequence, or contiguously if ``strict``)."""

    type: Literal["call_order"]
    tools: Annotated[list[str], Field(min_length=2)]
    strict: bool = False


class StateCriterion(FrozenModel):
    """A boolean expression over ``world`` (after), ``world_before`` and ``facts``.

    Evaluated with a sandboxed evaluator (simpleeval), never Python ``eval``.
    """

    type: Literal["state"]
    expr: Annotated[str, Field(min_length=1)]
    description: str | None = None


class JudgeCriterion(FrozenModel):
    """An LLM judge applies a versioned rubric, e.g. ``resolved`` or ``no_hallucination``."""

    type: Literal["judge"]
    rubric: Identifier
    n_judges: Literal[1, 3] = 1
    """3 runs a majority vote of three independent judgments."""
    model: ModelName | None = None
    """Overrides ``models.judge`` from ``hecklr.yaml`` for this criterion."""


class ContainsCriterion(FrozenModel):
    """Some message from ``speaker`` contains ``text``."""

    type: Literal["contains"]
    text: Annotated[str, Field(min_length=1)]
    speaker: Speaker = "agent"
    case_sensitive: bool = False


class NotContainsCriterion(FrozenModel):
    """No message from ``speaker`` contains ``text``."""

    type: Literal["not_contains"]
    text: Annotated[str, Field(min_length=1)]
    speaker: Speaker = "agent"
    case_sensitive: bool = False


class RegexCriterion(FrozenModel):
    """Some message from ``speaker`` matches ``pattern`` (Python ``re`` syntax, searched)."""

    type: Literal["regex"]
    pattern: Annotated[str, Field(min_length=1)]
    speaker: Speaker = "agent"
    ignore_case: bool = False


class MaxTurnsCriterion(FrozenModel):
    """The conversation resolves within ``limit`` agent turns."""

    type: Literal["max_turns"]
    limit: Annotated[int, Field(ge=1)]


class LatencyP95BelowCriterion(FrozenModel):
    """95th-percentile agent response latency in the trial is below ``ms``.

    Chat: time to first token. Voice: end of caller speech to first agent audio frame.
    """

    type: Literal["latency_p95_below"]
    ms: Annotated[int, Field(gt=0)]


class VoiceMetric(StrEnum):
    """Voice turn-taking metrics measured on the harness clock, each derived from trial events."""

    RESPONSE_LATENCY = "response_latency"
    """End of caller speech to first agent audio frame, in ms: ``latency_ms`` of agent
    ``utterance`` events."""
    BARGE_IN_REACTION = "barge_in_reaction"
    """Caller starts speaking over the agent to agent audio stopping, in ms: ``reaction_ms`` of
    user ``interrupt`` events with style ``barge_in`` (``None`` counts as never stopping)."""
    FALSE_INTERRUPTIONS = "false_interruptions"
    """Agent ``audio_span`` events (speech) that start inside a ``silence`` with reason
    ``hesitation``, per 100 user ``utterance`` events."""
    BACKCHANNEL_ROBUSTNESS = "backchannel_robustness"
    """Share of user ``interrupt`` events with style ``backchannel`` whose ``reaction_ms`` is set,
    i.e. the agent wrongly stopped; 0 to 1."""
    DEAD_AIR = "dead_air"
    """``silence`` events attributed to the agent with reason ``turn_gap`` lasting over 2 s, per
    call."""
    TALK_OVER_RATIO = "talk_over_ratio"
    """Seconds of overlap between user and agent speech ``audio_span`` events, per minute."""


class VoiceMetricCriterion(FrozenModel):
    """A voice metric of the trial stays below a threshold (in the metric's own unit)."""

    type: Literal["voice_metric"]
    metric: VoiceMetric
    below: Annotated[float, Field(gt=0)]
    quantile: Literal["p50", "p95", "p99"] = "p95"
    """Used by per-event latency metrics; ignored by rates and counts."""


class EmailThreadingCriterion(FrozenModel):
    """Every agent reply threads correctly (``In-Reply-To`` and ``References``, same subject)."""

    type: Literal["email_threading"]


class NoUnauthorizedRefundCriterion(FrozenModel):
    """No refund is issued unless every ``requires`` tool succeeded earlier in the trial."""

    type: Literal["no_unauthorized_refund"]
    refund_tool: str = "issue_refund"
    requires: list[str] = Field(default_factory=lambda: ["verify_customer"])


class NoCrossCustomerDataCriterion(FrozenModel):
    """The agent never reveals ``fields`` of a customer other than the caller."""

    type: Literal["no_cross_customer_data"]
    collection: Identifier = "customers"
    fields: list[str] = Field(default_factory=lambda: ["address", "phone", "email"])
    caller_fact: str = "customer_id"
    """Scenario fact holding the caller's own record key."""


class InjectionResistedCriterion(FrozenModel):
    """The agent did not follow injected instructions; ``forbidden`` strings never appear."""

    type: Literal["injection_resisted"]
    forbidden: list[str] = Field(default_factory=list)


class CustomCriterion(FrozenModel):
    """A grader registered under the ``hecklr.graders`` entry-point group."""

    type: Literal["custom"]
    name: Identifier
    params: dict[str, JsonValue] = Field(default_factory=dict)


SuccessCriterion = Annotated[
    ToolCalledCriterion
    | ToolNotCalledCriterion
    | ArgsMatchCriterion
    | CallOrderCriterion
    | StateCriterion
    | JudgeCriterion
    | ContainsCriterion
    | NotContainsCriterion
    | RegexCriterion
    | MaxTurnsCriterion
    | LatencyP95BelowCriterion
    | VoiceMetricCriterion
    | EmailThreadingCriterion
    | NoUnauthorizedRefundCriterion
    | NoCrossCustomerDataCriterion
    | InjectionResistedCriterion
    | CustomCriterion,
    Field(discriminator="type"),
]

# --- scenario -------------------------------------------------------------------------------


class SimulationMode(StrEnum):
    """How the simulated customer produces its turns."""

    SCRIPTED = "scripted"
    """Fixed ``user_turns``, for exact reproduction."""
    GUIDED = "guided"
    """Pursues the goal adaptively (default)."""
    REPLAY = "replay"
    """Goal and facts were extracted from a real transcript; runs like ``guided``."""
    ADVERSARIAL = "adversarial"
    """Follows the persona's named attack strategy."""


class ScenarioLimits(FrozenModel):
    """Hard stop conditions for one trial."""

    max_turns: Annotated[int, Field(ge=1)] = 24
    max_duration_s: Annotated[int, Field(ge=1)] = 300


class ScenarioSource(FrozenModel):
    """Where a scenario came from; provenance only, never read by graders."""

    kind: Literal["authored", "replay", "mined", "generated"] = "authored"
    conversation_id: str | None = None
    """``Conversation.id`` of the transcript a replay or mined scenario came from."""
    cluster_id: ClusterIdField | None = None
    note: str | None = None


class Scenario(FrozenModel):
    """One customer situation; crossed with personas, channels and trial indices into trials."""

    schema_version: SchemaVersion = 1
    id: Identifier
    title: str
    channels: Annotated[list[Channel], Field(min_length=1)]
    tags: list[Identifier] = Field(default_factory=list)
    persona: Identifier | Annotated[list[Identifier], Field(min_length=1)]
    """One persona id, or a list to cross the scenario with."""
    world: Identifier
    goal: Annotated[str, Field(min_length=1)]
    facts: dict[str, JsonValue] = Field(default_factory=dict)
    """Revealed by the simulator only when the agent asks or the persona would volunteer them."""
    success: Annotated[list[SuccessCriterion], Field(min_length=1)]
    limits: ScenarioLimits = ScenarioLimits()
    mode: SimulationMode = SimulationMode.GUIDED
    """``adversarial`` needs personas with an ``adversarial`` strategy; the suite loader checks
    this, since a scenario alone does not know its personas' definitions."""
    user_turns: list[str] | None = None
    """Required for, and only allowed in, ``scripted`` mode."""
    faults: list[ToolFault] = Field(default_factory=list)
    source: ScenarioSource | None = None

    @property
    def persona_ids(self) -> list[str]:
        """Persona ids to cross this scenario with, as a list."""
        return [self.persona] if isinstance(self.persona, str) else list(self.persona)

    @model_validator(mode="after")
    def _is_consistent(self) -> Self:
        if len(set(self.channels)) != len(self.channels):
            raise ValueError("channels must not repeat")
        if self.mode is SimulationMode.SCRIPTED and not self.user_turns:
            raise ValueError("scripted mode needs a non-empty user_turns list")
        if self.mode is not SimulationMode.SCRIPTED and self.user_turns is not None:
            raise ValueError("user_turns is only allowed in scripted mode")
        return self
