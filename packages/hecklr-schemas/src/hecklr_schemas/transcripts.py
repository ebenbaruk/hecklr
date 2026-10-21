"""Real conversations from production, in Hecklr's native JSONL format: one ``Conversation`` a line.

``hecklr replay`` turns them into replay scenarios and the miner ingests them; the miner's other
sources (OpenTelemetry, Langfuse, LangSmith, CSV) are converted into this model first.
"""

from typing import Annotated, Self

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from hecklr_schemas.common import Channel, FrozenModel, Identifier, NonNegativeMs, SchemaVersion
from hecklr_schemas.events import Party


class ConversationToolCall(FrozenModel):
    """A tool the agent called during a turn, with what it returned (when logged)."""

    tool: str
    args: dict[str, JsonValue] = Field(default_factory=dict)
    result: JsonValue = None
    error: str | None = None


class ConversationTurn(FrozenModel):
    """One message of a real conversation."""

    role: Party
    text: str
    t_ms: NonNegativeMs | None = None
    """Milliseconds since the conversation started, when the source logs times."""
    tool_calls: list[ConversationToolCall] = Field(default_factory=list)
    """Agent turns only."""


class Conversation(FrozenModel):
    """A logged conversation between a real customer and the agent."""

    schema_version: SchemaVersion = 1
    id: Annotated[str, Field(min_length=1)]
    """The source system's id, kept so clusters and replay scenarios can point back at it."""
    source: Identifier = "jsonl"
    """Where it was ingested from: ``jsonl``, ``otel``, ``langfuse``, ``langsmith`` or ``csv``."""
    channel: Channel = Channel.CHAT
    started_at: AwareDatetime | None = None
    turns: Annotated[list[ConversationTurn], Field(min_length=1)]
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    """Free-form source fields, e.g. outcome, CSAT or escalation flags."""

    @model_validator(mode="after")
    def _turns_are_consistent(self) -> Self:
        times = [turn.t_ms for turn in self.turns if turn.t_ms is not None]
        if times != sorted(times):
            raise ValueError("turn times must not go backwards")
        if any(turn.tool_calls for turn in self.turns if turn.role == "user"):
            raise ValueError("only agent turns can carry tool calls")
        return self
