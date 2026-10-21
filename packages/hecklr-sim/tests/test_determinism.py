"""Determinism: the same trial seed gives the same requests and the same turns (cache-friendly)."""

import anyio
from hypothesis import given, settings
from hypothesis import strategies as st

from hecklr_llm import FakeLLM, request_key
from hecklr_schemas import (
    Channel,
    DialogueTurn,
    LLMRequest,
    Persona,
    Scenario,
    SimEnd,
    SimTurn,
    TrialBrief,
    TrialId,
    new_id,
)
from hecklr_sim import derive_seed, make_simulator, unit_draw
from hecklr_sim.decision import SimDecision
from hecklr_sim.testing import decision_reply

_WORDS = ["Hi.", "C-1042 please.", "Next week?", "Mornings only.", "Okay.", "Hmm, why?"]
_AGENT = ["Your ID?", "Postcode?", "Tuesday 9am works?", "Sorry, could you repeat?"]


def _brief(seed: int, patience: float) -> TrialBrief:
    return TrialBrief(
        trial_id=TrialId(new_id()),
        scenario=Scenario.model_validate(
            {
                "id": "s",
                "title": "t",
                "channels": ["chat"],
                "persona": "p",
                "world": "w",
                "goal": "Move the visit",
                "facts": {"customer_id": "C-1042"},
                "success": [{"type": "judge", "rubric": "resolved"}],
                "limits": {"max_turns": 12},
            }
        ),
        persona=Persona.model_validate(
            {"id": "p", "description": "d", "traits": {"patience": patience}}
        ),
        channel=Channel.CHAT,
        seed=seed,
    )


def _seeded_reply(request: LLMRequest) -> SimDecision:
    """A reply that depends on the request seed, as a sampling model's would."""
    seed = request.seed or 0
    return decision_reply(_WORDS[seed % len(_WORDS)], progress=seed % 3 != 0)


async def _converse(brief: TrialBrief) -> tuple[list[SimTurn | SimEnd], list[str]]:
    llm = FakeLLM(responder=_seeded_reply)
    simulator = make_simulator("fake/sim")(brief, llm)
    dialogue: list[DialogueTurn] = []
    moves: list[SimTurn | SimEnd] = []
    for index in range(8):
        move = await simulator.next_turn(dialogue)
        moves.append(move)
        if isinstance(move, SimEnd):
            break
        dialogue += [
            DialogueTurn(speaker="user", text=move.text),
            DialogueTurn(speaker="agent", text=_AGENT[index % len(_AGENT)]),
        ]
    return moves, [request_key(call) for call in llm.calls]


@settings(max_examples=40, deadline=None)
@given(seed=st.integers(min_value=0, max_value=2**40), patience=st.floats(0.0, 1.0))
def test_ws2_same_seed_gives_the_same_turns_and_requests(seed: int, patience: float) -> None:
    first = anyio.run(_converse, _brief(seed, patience))
    second = anyio.run(_converse, _brief(seed, patience))

    assert first == second


def test_ws2_different_seeds_give_different_request_seeds() -> None:
    seeds = {derive_seed(seed, "turn", 0, 0) for seed in range(200)}
    assert len(seeds) == 200


def test_ws2_derived_seeds_are_stable_bounded_and_label_sensitive() -> None:
    # Pinned: a change here silently invalidates every user's response cache.
    assert derive_seed(42, "turn", 0, 0) == 1475150778
    assert derive_seed(42, "turn", 0, 0) != derive_seed(42, "turn", 0, 1)
    assert derive_seed(42, "turn", 0, 0) != derive_seed(42, "check", 0, 0)
    for seed in range(500):
        assert 0 <= derive_seed(seed, "x") < 2**31 - 1
        assert 0.0 <= unit_draw(seed, "x") < 1.0
