"""What the trace-based importers (OpenTelemetry, Langfuse, LangSmith) share.

Tracing tools log one record per agent invocation, not one per message: a record holds the
customer's latest message (often with the whole history before it) and the agent's reply, and tool
executions are separate child records. Each importer maps its records to ``Step``s; ``assemble``
turns the steps of one conversation into the frozen ``Conversation`` model.
"""

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pydantic import JsonValue, ValidationError

from hecklr_mine.errors import IngestError
from hecklr_schemas import Conversation, ConversationToolCall, ConversationTurn

Record = dict[str, JsonValue]

USER_ROLES: Final = frozenset({"user", "human", "customer"})
AGENT_ROLES: Final = frozenset({"assistant", "ai", "agent", "model", "bot"})
_CLASS_ROLES: Final = {"HumanMessage": "user", "AIMessage": "agent"}
"""LangChain serializes messages as constructors named after their class."""
_TEXT_KEYS: Final = ("content", "text", "message")
_MESSAGE_CONTAINERS: Final = (
    "messages", "message", "input", "inputs", "output", "outputs", "generations",
)  # fmt: skip
_LIST_CONTAINERS: Final = ("data", "runs", "traces", "observations", "items", "records")


@dataclass
class Step:
    """One agent invocation, or one tool execution, of a logged conversation."""

    conversation_id: str
    order: int
    """Position in the input file: breaks ties between records that share a start time."""
    start: datetime | None = None
    user: str | None = None
    """The newest customer message the invocation answered."""
    agent: str | None = None
    tool_calls: list[ConversationToolCall] = field(default_factory=list)
    metadata: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Message:
    """A chat message found in a logged input or output."""

    role: str
    """``user`` or ``agent``; other roles (system, tool) are dropped."""
    text: str


def read_records(path: Path) -> list[Record]:
    """Read a JSON array, a JSON object wrapping a list (API pages), or JSON Lines.

    Raises:
        IngestError: The file is not JSON, or holds something other than objects.
    """
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if not stripped:
        return []
    try:
        document: JsonValue = json.loads(text)
    except json.JSONDecodeError:
        return _json_lines(path, text)
    if isinstance(document, dict):
        nested = next((document[key] for key in _LIST_CONTAINERS if key in document), None)
        records = nested if isinstance(nested, list) else [document]
    elif isinstance(document, list):
        records = document
    else:
        raise IngestError(f"{path}: expected JSON objects, found {type(document).__name__}")
    return [_as_record(path, index + 1, item) for index, item in enumerate(records)]


def _json_lines(path: Path, text: str) -> list[Record]:
    records = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value: JsonValue = json.loads(line)
        except json.JSONDecodeError as exc:
            raise IngestError(f"{path}:{number}: not valid JSON ({exc.msg})") from exc
        records.append(_as_record(path, number, value))
    return records


def _as_record(path: Path, position: int, value: JsonValue) -> Record:
    if not isinstance(value, dict):
        raise IngestError(f"{path}: record {position} is not a JSON object")
    return value


def parse_time(value: JsonValue) -> datetime | None:
    """An ISO 8601 string or epoch nanoseconds as an aware datetime (naive times are UTC)."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1e9, tz=UTC)
    if not isinstance(value, str) or not value:
        return None
    if value.isdigit():
        return datetime.fromtimestamp(int(value) / 1e9, tz=UTC)
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def maybe_json(value: JsonValue) -> JsonValue:
    """Decode a string that holds JSON (APIs often return I/O as raw strings); else unchanged."""
    if isinstance(value, str) and value[:1] in ("{", "["):
        try:
            decoded: JsonValue = json.loads(value)
        except json.JSONDecodeError:
            return value
        return decoded
    return value


def as_object(value: JsonValue) -> dict[str, JsonValue]:
    """``value`` (or the JSON it holds) if it is an object, else an empty dict."""
    decoded = maybe_json(value)
    return decoded if isinstance(decoded, dict) else {}


def tool_args(value: JsonValue) -> dict[str, JsonValue]:
    """Logged tool arguments as a dict; a bare value is kept under ``input``."""
    decoded = maybe_json(value)
    if isinstance(decoded, dict):
        return decoded
    return {} if decoded is None else {"input": decoded}


def messages_in(value: JsonValue) -> list[Message]:
    """Chat messages in a logged input or output, in order, whatever the framework's format.

    Understands OpenAI-style ``{"role", "content"}``, OpenTelemetry ``{"role", "parts"}``,
    LangChain ``{"type": "human"}`` and serialized constructors, nested lists, and containers
    such as ``{"messages": [...]}``.
    """
    value = maybe_json(value)
    found: list[Message] = []
    if isinstance(value, list):
        for item in value:
            found.extend(messages_in(item))
    elif isinstance(value, dict):
        message = _message(value)
        if message is not None:
            found.append(message)
        else:
            for key in _MESSAGE_CONTAINERS:
                if key in value:
                    found.extend(messages_in(value[key]))
    return found


def _message(value: Mapping[str, JsonValue]) -> Message | None:
    kwargs, class_path = value.get("kwargs"), value.get("id")
    if isinstance(kwargs, dict) and isinstance(class_path, list):
        role = _CLASS_ROLES.get(str(class_path[-1]) if class_path else "")
        return Message(role, text_of(kwargs.get("content"))) if role else None
    raw_role = value.get("role", value.get("type"))
    if not isinstance(raw_role, str):
        return None
    if raw_role.lower() in USER_ROLES:
        role = "user"
    elif raw_role.lower() in AGENT_ROLES:
        role = "agent"
    else:
        return Message("other", "")
    content = value.get("content", value.get("parts", value.get("text")))
    return Message(role, text_of(content))


def text_of(value: JsonValue) -> str:
    """The plain text of a message body: a string, a list of parts, or a dict with text."""
    if isinstance(value, str) and value.startswith("["):
        value = maybe_json(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(text_of(part) for part in value if _is_text_part(part))
    if isinstance(value, dict):
        for key in _TEXT_KEYS:
            if isinstance(value.get(key), str | list):
                return text_of(value[key])
    return ""


def _is_text_part(part: JsonValue) -> bool:
    return isinstance(part, str) or (isinstance(part, dict) and part.get("type", "text") == "text")


def last_text(messages: Sequence[Message], role: str) -> str | None:
    """The newest non-empty message text of ``role``."""
    return next((m.text for m in reversed(messages) if m.role == role and m.text.strip()), None)


def first_string(value: JsonValue, keys: Iterable[str]) -> str | None:
    """``value`` itself if it is a string, else its first string field among ``keys``."""
    value = maybe_json(value)
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        for key in keys:
            field_value = value.get(key)
            if isinstance(field_value, str) and field_value:
                return field_value
    return None


def assemble(steps: Iterable[Step], *, source: str) -> list[Conversation]:
    """Group steps by conversation and build one ``Conversation`` each, in first-seen order.

    Within a conversation, steps run in time order (file order breaks ties). A step's customer
    message is skipped when it repeats the previous turn (an agent loop re-sends it to the model).
    Tool calls are attached to the agent turn of their step, or to the next agent turn when their
    step has no reply; calls after the last reply get an empty agent turn of their own.

    Raises:
        IngestError: A conversation has no message at all, or fails validation.
    """
    grouped: dict[str, list[Step]] = defaultdict(list)
    for step in steps:
        grouped[step.conversation_id].append(step)
    return [_conversation(cid, group, source) for cid, group in grouped.items()]


def _conversation(conversation_id: str, steps: list[Step], source: str) -> Conversation:
    steps.sort(key=lambda s: (s.start or datetime.min.replace(tzinfo=UTC), s.order))
    timed = all(step.start is not None for step in steps)
    origin = steps[0].start if timed else None
    turns: list[ConversationTurn] = []
    pending: list[ConversationToolCall] = []
    merged: dict[str, JsonValue] = {}
    for step in steps:
        merged.update(step.metadata)
        t_ms = _offset_ms(origin, step.start)
        user = (step.user or "").strip()
        if user and not (turns and turns[-1].role == "user" and turns[-1].text == user):
            turns.append(ConversationTurn(role="user", text=user, t_ms=t_ms))
        pending.extend(step.tool_calls)
        agent = (step.agent or "").strip()
        if agent:
            turns.append(ConversationTurn(role="agent", text=agent, t_ms=t_ms, tool_calls=pending))
            pending = []
    if pending:
        last_ms = _offset_ms(origin, steps[-1].start)
        turns.append(ConversationTurn(role="agent", text="", t_ms=last_ms, tool_calls=pending))
    if not turns:
        raise IngestError(f"conversation {conversation_id!r} ({source}) has no messages")
    try:
        return Conversation(
            id=conversation_id,
            source=source,
            started_at=origin,
            turns=turns,
            metadata=merged,
        )
    except ValidationError as exc:
        raise IngestError(f"conversation {conversation_id!r} ({source}): {exc}") from exc


def _offset_ms(origin: datetime | None, moment: datetime | None) -> int | None:
    if origin is None or moment is None:
        return None
    return round((moment - origin).total_seconds() * 1000)
