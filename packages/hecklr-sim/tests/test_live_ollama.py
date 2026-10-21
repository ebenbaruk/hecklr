"""Guided mode and replay extraction against a real local model. Opt in with ``pytest -m live``.

Free (local Ollama, ADR 0005), so no budget. Assertions check the plumbing and the guard's
guarantees (valid structured turns, never a wrong customer ID), not conversational quality.
"""

import os
import re
from collections.abc import Sequence

import httpx2
import pytest

from hecklr_llm import ProviderRouter
from hecklr_schemas import (
    Channel,
    Conversation,
    DialogueTurn,
    Persona,
    Scenario,
    SimEnd,
    SimTurn,
    TrialBrief,
    TrialId,
    new_id,
)
from hecklr_sim import GuidedSimulator, extract_scenario, make_simulator

MODEL = os.environ.get("HECKLR_LIVE_OLLAMA_MODEL", "ollama/qwen3:4b-instruct")
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")

pytestmark = pytest.mark.live

AGENT_SCRIPT = [
    "Hello, Acme Maintenance. How can I help you today?",
    "I can help with that. What is your customer ID, please?",
    "Thanks. And your postcode?",
    "I have Tuesday next week at 9am. Shall I book it?",
    "Done: your visit is moved to Tuesday at 9am. Anything else?",
]


def _ollama_has_model() -> bool:
    try:
        response = httpx2.get(f"{OLLAMA_URL}/models", timeout=2.0)
    except httpx2.HTTPError:
        return False
    names = {entry["id"] for entry in response.json().get("data", [])}
    return MODEL.removeprefix("ollama/") in names


@pytest.fixture(scope="module", autouse=True)
def _require_ollama() -> None:
    if not _ollama_has_model():
        pytest.skip(f"Ollama at {OLLAMA_URL} with {MODEL} is not available")


def _brief() -> TrialBrief:
    return TrialBrief(
        trial_id=TrialId(new_id()),
        scenario=Scenario.model_validate(
            {
                "id": "reschedule-boiler-visit",
                "title": "Customer moves a boiler maintenance visit",
                "channels": ["chat"],
                "persona": "busy-homeowner",
                "world": "acme-maintenance",
                "goal": "Move Thursday's boiler visit to next week, mornings only",
                "facts": {"customer_id": "C-1042", "postcode": "92000"},
                "success": [{"type": "judge", "rubric": "resolved"}],
                "limits": {"max_turns": 12},
            }
        ),
        persona=Persona.model_validate(
            {
                "id": "busy-homeowner",
                "description": "Calls from a car, short on time, mildly annoyed",
                "traits": {"patience": 0.6, "verbosity": 0.3, "cooperativeness": 0.8},
            }
        ),
        channel=Channel.CHAT,
        seed=2026,
    )


async def _converse(simulator: GuidedSimulator) -> tuple[list[DialogueTurn], SimEnd | None]:
    dialogue: list[DialogueTurn] = [DialogueTurn(speaker="agent", text=AGENT_SCRIPT[0])]
    for agent_line in AGENT_SCRIPT[1:]:
        move = await simulator.next_turn(dialogue)
        if isinstance(move, SimEnd):
            return dialogue, move
        dialogue.append(DialogueTurn(speaker="user", text=move.text))
        dialogue.append(DialogueTurn(speaker="agent", text=agent_line))
    final = await simulator.next_turn(dialogue)
    return dialogue, final if isinstance(final, SimEnd) else None


def _customer_ids(turns: Sequence[DialogueTurn]) -> set[str]:
    text = " ".join(turn.text for turn in turns if turn.speaker == "user")
    return {match.upper() for match in re.findall(r"\bC-\d{4}\b", text, flags=re.IGNORECASE)}


@pytest.mark.anyio
async def test_live_guided_customer_converses_and_never_gives_a_wrong_id() -> None:
    async with ProviderRouter() as router:
        simulator = make_simulator(MODEL, temperature=0.0, max_regenerations=3)(_brief(), router)
        assert isinstance(simulator, GuidedSimulator)
        dialogue, end = await _converse(simulator)

    user_turns = [turn for turn in dialogue if turn.speaker == "user"]
    assert user_turns, "the simulator never spoke"
    assert all(turn.text.strip() for turn in user_turns)
    assert _customer_ids(dialogue) <= {"C-1042"}
    assert simulator.contradictions >= 0
    if end is not None:
        assert end.reason.value in {"goal_achieved", "goal_abandoned", "handoff"}


@pytest.mark.anyio
async def test_live_guided_first_turn_is_reproducible_through_the_cache_key() -> None:
    async with ProviderRouter() as router:
        first = await make_simulator(MODEL, temperature=0.0)(_brief(), router).next_turn([])
        second = await make_simulator(MODEL, temperature=0.0)(_brief(), router).next_turn([])

    assert isinstance(first, SimTurn)
    assert first == second


@pytest.mark.anyio
async def test_live_replay_extraction_grounds_facts() -> None:
    conversation = Conversation.model_validate(
        {
            "id": "live-1",
            "turns": [
                {"role": "user", "text": "Hi, I need to move my boiler service on Thursday."},
                {"role": "agent", "text": "Sure. Your customer ID and postcode?"},
                {"role": "user", "text": "C-1042, postcode 92000"},
                {"role": "agent", "text": "Done, moved to Tuesday 9am."},
            ],
        }
    )
    async with ProviderRouter() as router:
        scenario = await extract_scenario(
            conversation, router, model=MODEL, world="acme-maintenance", persona="caller"
        )

    assert scenario.goal
    customer_text = "Hi, I need to move my boiler service on Thursday. C-1042, postcode 92000"
    assert all(
        str(value).casefold() in customer_text.casefold() for value in scenario.facts.values()
    )
