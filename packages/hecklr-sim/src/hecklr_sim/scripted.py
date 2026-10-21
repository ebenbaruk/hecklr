"""Scripted mode: the customer sends the scenario's fixed ``user_turns``, for exact reproduction."""

from collections.abc import Sequence

from hecklr_schemas import (
    Channel,
    Clock,
    DialogueTurn,
    EndReason,
    SimEnd,
    SimTurn,
    TrialBrief,
)
from hecklr_sim.limits import limit_reached, user_turn_count


class ScriptedSimulator:
    """Sends ``scenario.user_turns`` in order, one per call, and never calls a model.

    The position in the script is the number of customer messages already in the dialogue, so
    the simulator holds no state and replays identically. When the script runs out the customer
    ends with ``goal_achieved``: it has said everything it came to say, and whether the agent
    actually succeeded is for the graders, not the end reason, to decide.
    """

    def __init__(self, brief: TrialBrief, clock: Clock) -> None:
        if not brief.scenario.user_turns:
            raise ValueError(f"scenario {brief.scenario.id!r} has no user_turns to script")
        self._brief = brief
        self._lines = list(brief.scenario.user_turns)
        self._clock = clock
        self._start_ms = clock.now_ms()

    @property
    def contradictions(self) -> int:
        """Always 0: scripted turns come from the scenario, so they cannot contradict it."""
        return 0

    async def next_turn(self, dialogue: Sequence[DialogueTurn]) -> SimTurn | SimEnd:
        """Return the next scripted line, or end when the script or a limit runs out."""
        reason = limit_reached(
            dialogue, self._brief.scenario.limits, self._clock.now_ms() - self._start_ms
        )
        if reason is not None:
            return SimEnd(reason=reason)
        position = user_turn_count(dialogue)
        if position >= len(self._lines):
            return SimEnd(reason=EndReason.GOAL_ACHIEVED)
        subject = (
            self._brief.scenario.title
            if self._brief.channel is Channel.EMAIL and position == 0
            else None
        )
        return SimTurn(text=self._lines[position], subject=subject)
