"""The Grader Protocol shared by built-in and custom (entry-point) graders."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from hecklr_schemas.common import ModelName
from hecklr_schemas.events import Event
from hecklr_schemas.llm import LLMProvider
from hecklr_schemas.persona import Persona
from hecklr_schemas.runtime import Grade, Trial
from hecklr_schemas.scenario import Scenario, SuccessCriterion

GRADER_ENTRY_POINT_GROUP = "hecklr.graders"
"""Custom graders register under this ``importlib.metadata`` entry-point group."""


@dataclass(frozen=True, slots=True)
class GradeContext:
    """Everything a grader may read about one finished trial.

    World snapshots are on ``trial`` (``world_before`` / ``world_after``).
    """

    trial: Trial
    events: Sequence[Event]
    scenario: Scenario
    persona: Persona
    criterion: SuccessCriterion | None = None
    """The success criterion being graded; ``None`` for diagnostic graders."""
    criterion_index: int | None = None
    llm: LLMProvider | None = field(default=None, repr=False)
    """Model access for judges; ``None`` for deterministic graders."""
    judge_model: ModelName | None = None
    """Default ``provider/model`` for judges, from ``hecklr.yaml``."""


@runtime_checkable
class Grader(Protocol):
    """Turns one trial into one ``Grade``.

    ``version`` must change whenever the grader's logic or prompt changes, so cached grades and
    judge calibration never silently mix versions.
    """

    @property
    def name(self) -> str:
        """Stable grader name, e.g. ``tool_called`` or ``judge:resolved``."""
        ...

    @property
    def version(self) -> str:
        """Version of the grading logic or rubric prompt."""
        ...

    async def grade(self, ctx: GradeContext) -> Grade:
        """Grade one trial."""
        ...
