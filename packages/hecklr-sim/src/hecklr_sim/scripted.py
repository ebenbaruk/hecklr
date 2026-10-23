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
from hecklr_sim.limits import limit_reached


class ScriptedSimulator:
    """Sends ``scenario.user_turns`` in order, one per call, and never calls a model.

    The position in the script is the number of script lines already said, read from the
    dialogue, so the simulator holds no state and replays identically. Only user turns that are
    the next line count: over voice a barge-in adds an interjection ("Wait, hold on a second.",
    ADR 0009a) as a user turn of its own, which must not skip a line (issue #44).

    When the script runs out the customer ends with ``goal_achieved``: it has said everything it
    came to say, and whether the agent actually succeeded is for the graders, not the end reason,
    to decide.
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
        position = _lines_said(self._lines, dialogue)
        if position >= len(self._lines):
            return SimEnd(reason=EndReason.GOAL_ACHIEVED)
        subject = (
            self._brief.scenario.title
            if self._brief.channel is Channel.EMAIL and position == 0
            else None
        )
        return SimTurn(text=self._lines[position], subject=subject)


def _lines_said(lines: Sequence[str], dialogue: Sequence[DialogueTurn]) -> int:
    """How many of ``lines`` the customer has said, matched in order against its turns."""
    said = 0
    for turn in dialogue:
        if turn.speaker == "user" and said < len(lines) and _same(turn.text, lines[said]):
            said += 1
    return said


def _same(spoken: str, line: str) -> bool:
    # Voice splits a hesitant line in two and rejoins the halves with single spaces.
    return spoken.split() == line.split()
