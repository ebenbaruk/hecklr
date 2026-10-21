"""The world sandbox: a stateful mock backend the agent's tools act on.

A world is declarative on purpose. Each collection holds records keyed by one field; each tool is
either one CRUD operation over a collection, where tool parameters map to record fields by name,
or a Python entry point for logic that does not fit that shape (see ADR 0004).
"""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, StringConstraints, model_validator

from hecklr_schemas.common import (
    FrozenModel,
    Identifier,
    NonNegativeMs,
    Probability,
    SchemaVersion,
)

EntryPoint = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$"),
]
"""A ``module.path:function`` reference loaded with importlib."""

JsonObject = dict[str, JsonValue]
Record = dict[str, JsonValue]


class CollectionDef(FrozenModel):
    """One table of the world, e.g. ``customers`` or ``appointments``."""

    key: Identifier = "id"
    """Field whose value identifies a record; state expressions index collections by it."""
    description: str | None = None
    record_schema: JsonObject | None = None
    """Optional JSON Schema every record must satisfy."""
    records: list[Record] = Field(default_factory=list)
    """Inline seed data."""
    seed_file: str | None = None
    """Path (relative to the world file) to a JSON, YAML or CSV file with more seed records."""

    @model_validator(mode="after")
    def _records_carry_their_key(self) -> Self:
        for index, record in enumerate(self.records):
            if self.key not in record:
                raise ValueError(f"record {index} is missing key field {self.key!r}")
        return self


CollectionOpKind = Literal["get", "list", "find", "create", "update", "delete"]


class CollectionOp(FrozenModel):
    """A tool implemented as one operation over a collection.

    - ``get`` / ``delete``: the record whose key equals parameter ``key_param``.
    - ``list``: every record whose fields equal the supplied parameters (all records if none).
    - ``find``: like ``list`` but returns the first match or a not-found error.
    - ``create``: inserts the parameters as a new record (the key must be present or generated).
    - ``update``: sets every parameter except ``key_param`` on the record named by ``key_param``.
    """

    kind: CollectionOpKind
    collection: Identifier
    key_param: Identifier | None = None
    """Tool parameter carrying the record key; defaults to the collection's key field."""


class PythonOp(FrozenModel):
    """A tool implemented by a Python function ``(world, **params) -> result``."""

    kind: Literal["python"]
    entrypoint: EntryPoint


ToolOperation = Annotated[CollectionOp | PythonOp, Field(discriminator="kind")]


def _empty_object_schema() -> JsonObject:
    return {"type": "object", "properties": {}}


class ToolDef(FrozenModel):
    """A tool the agent can call, as exposed over MCP and the OpenAPI server."""

    name: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=64)]
    description: str
    params: JsonObject = Field(default_factory=_empty_object_schema)
    """JSON Schema of the tool arguments (an ``object`` schema)."""
    op: ToolOperation

    @model_validator(mode="after")
    def _params_is_object_schema(self) -> Self:
        if self.params.get("type") != "object":
            raise ValueError(f"tool {self.name!r}: params must be a JSON Schema of type 'object'")
        return self


class World(FrozenModel):
    """A world definition loaded fresh into an in-memory store for every trial."""

    schema_version: SchemaVersion = 1
    id: Identifier
    description: str | None = None
    collections: dict[Identifier, CollectionDef]
    tools: list[ToolDef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _tools_are_consistent(self) -> Self:
        names = [tool.name for tool in self.tools]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate tool names: {duplicates}")
        for tool in self.tools:
            if isinstance(tool.op, CollectionOp) and tool.op.collection not in self.collections:
                raise ValueError(
                    f"tool {tool.name!r} targets unknown collection {tool.op.collection!r}"
                )
        return self


class FaultKind(StrEnum):
    """Failure a scenario injects into a world tool to test agent resilience."""

    TIMEOUT = "timeout"
    """The call is held until the trial ends; the agent's own timeout must fire."""
    HTTP_500 = "http_500"
    """The call fails with HTTP status 500 (an MCP tool error on the MCP server)."""
    SLOW = "slow"
    """The call succeeds after ``delay_ms``."""
    STALE_DATA = "stale_data"
    """The call succeeds but ``patch`` overrides fields of the returned record(s)."""


class ToolFault(FrozenModel):
    """A fault injected into one tool for one scenario."""

    tool: str
    kind: FaultKind
    probability: Probability = 1.0
    """Chance each call is affected; sampled from the trial's seeded RNG."""
    after_calls: Annotated[int, Field(ge=0)] = 0
    """Number of calls that succeed normally before the fault starts."""
    delay_ms: NonNegativeMs | None = None
    patch: Record | None = None

    @model_validator(mode="after")
    def _kind_has_its_parameters(self) -> Self:
        if (self.kind is FaultKind.SLOW) != (self.delay_ms is not None):
            raise ValueError("delay_ms is required for, and only allowed in, a 'slow' fault")
        if (self.kind is FaultKind.STALE_DATA) != bool(self.patch):
            raise ValueError("a non-empty patch is required for, and only allowed in, 'stale_data'")
        return self


class WorldSnapshot(FrozenModel):
    """Full world state at one instant: collection -> record key -> record.

    Record keys are always strings (JSON object keys), so a record whose key field is the integer
    ``1`` is ``collections["orders"]["1"]``; state expressions index with the string form.
    """

    world_id: Identifier
    taken_at_ms: NonNegativeMs
    """Trial clock time of the snapshot (ms since the trial started, like ``Event.t_ms``)."""
    collections: dict[str, dict[str, Record]]
