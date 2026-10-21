"""Building blocks shared by every schema module."""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

SCHEMA_VERSION: Final = 1
"""Current version of every authored and produced schema. Bumped only through an RFC."""

SchemaVersion = Literal[1]


class FrozenModel(BaseModel):
    """Base for every Hecklr model.

    Frozen because artifacts are append-only: a record is built once and never mutated (use
    ``model_copy(update=...)`` to derive a new one). ``extra="forbid"`` makes a typo in a YAML file
    fail fast instead of being silently ignored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)


Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", min_length=1, max_length=128),
]
"""A human-authored id such as a scenario, persona, world, tool or rubric name."""

ModelName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]*/\S+$")]
"""A model reference in ``provider/model`` form, e.g. ``anthropic/claude-sonnet-5-5``."""

Probability = Annotated[float, Field(ge=0.0, le=1.0)]
NonNegativeMs = Annotated[int, Field(ge=0)]


class Channel(StrEnum):
    """Transport a conversation runs over."""

    CHAT = "chat"
    VOICE = "voice"
    EMAIL = "email"


Actor = Literal["user", "agent", "tool", "system"]
"""Who produced an event: the simulated customer, the agent under test, a world tool or Hecklr."""


class Budget(FrozenModel):
    """Spending and concurrency limits for a run."""

    usd: Annotated[Decimal, Field(ge=0)] | None = None
    """Hard cost cap for model calls; ``None`` means unlimited."""
    concurrency: Annotated[int, Field(ge=1)] = 32
    """Maximum number of trials in flight at once."""
