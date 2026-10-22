"""Stage 1, Ingest: log exports in any supported format become frozen ``Conversation``s."""

import csv
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from hecklr_mine.errors import IngestError
from hecklr_mine.langfuse import load_langfuse
from hecklr_mine.langsmith import load_langsmith
from hecklr_mine.otel import load_otel
from hecklr_mine.steps import parse_time
from hecklr_schemas import Channel, Conversation, ConversationTurn
from hecklr_schemas.events import Party

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


class LogFormat(StrEnum):
    """Log export formats the miner reads."""

    JSONL = "jsonl"
    """Hecklr's native format: one ``Conversation`` per line."""
    OTEL = "otel"
    LANGFUSE = "langfuse"
    LANGSMITH = "langsmith"
    CSV = "csv"


DEFAULT_ROLE_VALUES: Final[Mapping[str, Party]] = {
    "user": "user",
    "customer": "user",
    "human": "user",
    "agent": "agent",
    "assistant": "agent",
    "bot": "agent",
    "ai": "agent",
}


class CsvMapping(BaseModel):
    """Which CSV columns hold what: one row per message, rows grouped by conversation.

    Written as YAML for ``hecklr mine --mapping``; every key is optional.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: str = "conversation_id"
    role: str = "role"
    text: str = "text"
    timestamp: str | None = None
    """ISO 8601 times; rows are sorted by it within a conversation. Naive times are UTC."""
    channel: str | None = None
    metadata: list[str] = Field(default_factory=list)
    """Columns copied into ``Conversation.metadata`` (first row of the conversation wins)."""
    role_values: Annotated[dict[str, Party], Field(min_length=1)] = Field(
        default_factory=lambda: dict(DEFAULT_ROLE_VALUES)
    )
    """Cell value (case-insensitive) -> ``user`` or ``agent``."""
    delimiter: Annotated[str, Field(min_length=1, max_length=1)] = ","

    @classmethod
    def from_yaml(cls, path: Path) -> "CsvMapping":
        """Load a mapping file.

        Raises:
            IngestError: The file is not valid YAML or not a valid mapping.
        """
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return cls.model_validate(data)
        except (yaml.YAMLError, ValidationError) as exc:
            raise IngestError(f"{path}: invalid CSV mapping: {exc}") from exc


def load_logs(
    path: Path, log_format: LogFormat, *, mapping: CsvMapping | None = None
) -> list[Conversation]:
    """Read ``path`` in ``log_format``; ``mapping`` applies to CSV only.

    Raises:
        IngestError: The file cannot be read or converted; the message names the record.
    """
    if not path.is_file():
        raise IngestError(f"{path}: no such file")
    loaders: dict[LogFormat, Callable[[Path], list[Conversation]]] = {
        LogFormat.JSONL: load_jsonl,
        LogFormat.OTEL: load_otel,
        LogFormat.LANGFUSE: load_langfuse,
        LogFormat.LANGSMITH: load_langsmith,
        LogFormat.CSV: lambda p: load_csv(p, mapping or CsvMapping()),
    }
    conversations = loaders[log_format](path)
    _check_unique(path, conversations)
    return conversations


def load_jsonl(path: Path) -> list[Conversation]:
    """Read Hecklr's native JSONL, one ``Conversation`` per non-blank line."""
    conversations = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                conversations.append(Conversation.model_validate_json(line))
            except ValidationError as exc:
                raise IngestError(f"{path}:{number}: not a valid conversation: {exc}") from exc
    return conversations


def load_csv(path: Path, mapping: CsvMapping) -> list[Conversation]:
    """Read one message per row, grouped into conversations in first-seen order."""
    roles = {value.casefold(): party for value, party in mapping.role_values.items()}
    rows: dict[str, list[tuple[int, dict[str, str]]]] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=mapping.delimiter)
        _check_columns(path, mapping, reader.fieldnames or [])
        for number, row in enumerate(reader, 2):
            rows.setdefault(row[mapping.conversation_id], []).append((number, row))
    return [_csv_conversation(path, cid, group, mapping, roles) for cid, group in rows.items()]


def _check_columns(path: Path, mapping: CsvMapping, header: Sequence[str]) -> None:
    wanted = [mapping.conversation_id, mapping.role, mapping.text, *mapping.metadata]
    wanted += [column for column in (mapping.timestamp, mapping.channel) if column is not None]
    missing = [column for column in wanted if column not in header]
    if missing:
        raise IngestError(f"{path}: missing CSV column(s) {missing}; header is {list(header)}")


def _csv_conversation(
    path: Path,
    conversation_id: str,
    rows: list[tuple[int, dict[str, str]]],
    mapping: CsvMapping,
    roles: Mapping[str, Party],
) -> Conversation:
    timed = [(number, row, _csv_time(path, number, row, mapping)) for number, row in rows]
    # A stable sort, so rows sharing a time (or untimed rows) keep their file order.
    timed.sort(key=lambda item: item[2] or _EPOCH)
    origin = timed[0][2]
    turns = []
    for number, row, moment in timed:
        role = roles.get(row[mapping.role].strip().casefold())
        if role is None:
            raise IngestError(
                f"{path}:{number}: unknown role {row[mapping.role]!r}; "
                f"map it under role_values ({sorted(roles)})"
            )
        t_ms = round((moment - origin).total_seconds() * 1000) if moment and origin else None
        turns.append(ConversationTurn(role=role, text=row[mapping.text], t_ms=t_ms))
    first = timed[0][1]
    metadata: dict[str, JsonValue] = {column: first[column] for column in mapping.metadata}
    try:
        return Conversation(
            id=conversation_id,
            source="csv",
            channel=Channel(first[mapping.channel]) if mapping.channel else Channel.CHAT,
            started_at=origin,
            turns=turns,
            metadata=metadata,
        )
    except (ValidationError, ValueError) as exc:
        raise IngestError(f"{path}: conversation {conversation_id!r}: {exc}") from exc


def _csv_time(
    path: Path, number: int, row: Mapping[str, str], mapping: CsvMapping
) -> datetime | None:
    if mapping.timestamp is None:
        return None
    moment = parse_time(row[mapping.timestamp])
    if moment is None:
        raise IngestError(f"{path}:{number}: unreadable timestamp {row[mapping.timestamp]!r}")
    return moment


def _check_unique(path: Path, conversations: list[Conversation]) -> None:
    seen: set[str] = set()
    for conversation in conversations:
        if conversation.id in seen:
            raise IngestError(f"{path}: conversation id {conversation.id!r} appears twice")
        seen.add(conversation.id)
