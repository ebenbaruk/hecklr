"""Shared fixtures for hecklr-sim tests: trial briefs and dialogues."""

from collections.abc import Callable
from typing import Any

import pytest

from hecklr_schemas import Channel, DialogueTurn, Persona, Scenario, TrialBrief, TrialId, new_id

BriefFactory = Callable[..., TrialBrief]
DialogueFactory = Callable[..., list[DialogueTurn]]

SCENARIO: dict[str, Any] = {
    "id": "reschedule-boiler-visit",
    "title": "Customer moves a boiler maintenance visit",
    "channels": ["chat", "voice", "email"],
    "persona": "busy-homeowner",
    "world": "acme-maintenance",
    "goal": "Move Thursday's visit to next week, mornings only",
    "facts": {"customer_id": "C-1042", "postcode": "92000", "current_slot": "2026-10-23T09:00"},
    "success": [{"type": "judge", "rubric": "resolved"}],
    "limits": {"max_turns": 24, "max_duration_s": 300},
}

PERSONA: dict[str, Any] = {
    "id": "busy-homeowner",
    "description": "Calls from a car, short on time, mildly annoyed",
    "language": "en-GB",
    "traits": {"patience": 0.4, "verbosity": 0.3, "cooperativeness": 0.8},
}


def _make_brief(
    *,
    seed: int = 7,
    channel: Channel = Channel.CHAT,
    scenario: dict[str, Any] | None = None,
    persona: dict[str, Any] | None = None,
) -> TrialBrief:
    return TrialBrief(
        trial_id=TrialId(new_id()),
        scenario=Scenario.model_validate({**SCENARIO, **(scenario or {})}),
        persona=Persona.model_validate({**PERSONA, **(persona or {})}),
        channel=channel,
        seed=seed,
    )


def _dialogue(*lines: str) -> list[DialogueTurn]:
    turns = []
    for line in lines:
        speaker, text = line.split(": ", 1)
        assert speaker in ("user", "agent"), line
        turns.append(DialogueTurn.model_validate({"speaker": speaker, "text": text}))
    return turns


@pytest.fixture
def make_brief() -> BriefFactory:
    """The spec's example scenario and persona with overrides: ``make_brief(seed=3, ...)``."""
    return _make_brief


@pytest.fixture
def say() -> DialogueFactory:
    """Build a dialogue from lines: ``say("agent: Hello", "user: Hi")``."""
    return _dialogue
