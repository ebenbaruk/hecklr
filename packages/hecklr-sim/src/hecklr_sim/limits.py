"""Hard stop conditions shared by every simulator mode."""

from collections.abc import Sequence

from hecklr_schemas import DialogueTurn, EndReason, ScenarioLimits


def limit_reached(
    dialogue: Sequence[DialogueTurn], limits: ScenarioLimits, elapsed_ms: int
) -> EndReason | None:
    """Return why the customer must stop now, or ``None`` if it may send another message.

    ``max_turns`` counts the messages of both parties, like ``DriverResult.turns``: the customer
    stops when its next message would exceed it. The channel driver enforces the same limits
    mid-turn; checking here lets the customer leave cleanly with a structured reason.
    """
    if len(dialogue) + 1 > limits.max_turns:
        return EndReason.MAX_TURNS
    if elapsed_ms >= limits.max_duration_s * 1000:
        return EndReason.MAX_DURATION
    return None


def user_turn_count(dialogue: Sequence[DialogueTurn]) -> int:
    """How many messages the customer has sent so far."""
    return sum(1 for turn in dialogue if turn.speaker == "user")
