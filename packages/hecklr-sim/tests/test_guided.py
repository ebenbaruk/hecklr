"""Guided mode (and replay, which runs as guided): brief, decisions, patience and limits."""

from collections.abc import Callable

import pytest

from hecklr_llm import FakeLLM
from hecklr_schemas import (
    Channel,
    CustomerSimulator,
    DialogueTurn,
    EndReason,
    FakeClock,
    SimEnd,
    SimTurn,
    SimulatorFactory,
    Traits,
    TrialBrief,
)
from hecklr_sim import (
    GuidedSimulator,
    Patience,
    SimulatorError,
    derive_subseed,
    make_adversarial_simulator,
    make_simulator,
)
from hecklr_sim.decision import LLMEndReason
from hecklr_sim.guided import DEFAULT_MAX_TOKENS
from hecklr_sim.testing import decision_reply

BriefFactory = Callable[..., TrialBrief]
DialogueFactory = Callable[..., list[DialogueTurn]]


@pytest.mark.anyio
async def test_ws2_guided_speaks_what_the_llm_decides(make_brief: BriefFactory) -> None:
    llm = FakeLLM(script=[decision_reply("  Hi, I need to move my boiler visit.  ")])
    simulator = make_simulator("fake/sim")(make_brief(), llm)

    turn = await simulator.next_turn([])

    assert isinstance(simulator, GuidedSimulator)
    assert isinstance(simulator, CustomerSimulator)
    assert turn == SimTurn(text="Hi, I need to move my boiler visit.")
    assert simulator.contradictions == 0


@pytest.mark.anyio
async def test_ws2_guided_request_carries_the_private_brief_and_a_derived_seed(
    make_brief: BriefFactory, say: DialogueFactory
) -> None:
    brief = make_brief(seed=11)
    llm = FakeLLM(script=[decision_reply("C-1042", facts_revealed=["customer_id"])])
    simulator = make_simulator("fake/sim", temperature=0.7, max_tokens=300)(brief, llm)

    await simulator.next_turn(say("user: Hi, I need to move my visit.", "agent: Your ID?"))

    (request,) = llm.calls
    assert request.model == "fake/sim"
    assert request.seed == derive_subseed(11, "turn", 1, 0)
    assert request.temperature == 0.7
    assert request.max_tokens == 300
    assert request.schema_name == "SimDecision"
    system = request.system or ""
    for expected in (
        "Calls from a car, short on time, mildly annoyed",
        "Move Thursday's visit to next week, mornings only",
        "customer_id: C-1042",
        "postcode: 92000",
        "Reveal a fact only when the agent asks for it",
        "en-GB",
        "Keep every message very short",
    ):
        assert expected in system
    prompt = request.messages[-1].content
    assert "YOU: Hi, I need to move my visit." in prompt
    assert "AGENT: Your ID?" in prompt


@pytest.mark.anyio
async def test_ws2_information_discipline_simulator_sees_only_visible_dialogue(
    make_brief: BriefFactory, say: DialogueFactory
) -> None:
    llm = FakeLLM(script=[decision_reply("Okay.")])
    simulator = make_simulator("fake/sim")(make_brief(), llm)
    dialogue = say("user: Hi", "agent: Let me look that up for you.")

    await simulator.next_turn(dialogue)

    prompt = llm.calls[0].messages[-1].content
    transcript = prompt.split("## Your state")[0]
    assert transcript.splitlines()[2:] == ["YOU: Hi", "AGENT: Let me look that up for you.", ""]
    assert len(llm.calls[0].messages) == 1


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("goal_achieved", EndReason.GOAL_ACHIEVED),
        ("handoff", EndReason.HANDOFF),
        ("goal_abandoned", EndReason.GOAL_ABANDONED),
    ],
)
@pytest.mark.anyio
async def test_ws2_guided_ends_with_the_reason_the_customer_gives(
    make_brief: BriefFactory, say: DialogueFactory, reason: LLMEndReason, expected: EndReason
) -> None:
    llm = FakeLLM(script=[decision_reply("Great, thanks, bye.", action="end", end_reason=reason)])
    simulator = make_simulator("fake/sim")(make_brief(), llm)

    end = await simulator.next_turn(say("user: Hi", "agent: Done, moved to Tuesday 9am."))

    assert end == SimEnd(reason=expected, closing_text="Great, thanks, bye.")


@pytest.mark.anyio
async def test_ws2_guided_end_without_closing_text(
    make_brief: BriefFactory, say: DialogueFactory
) -> None:
    llm = FakeLLM(script=[decision_reply("", action="end", end_reason="handoff")])
    simulator = make_simulator("fake/sim")(make_brief(), llm)

    end = await simulator.next_turn(say("user: Hi", "agent: Transferring you to a colleague."))

    assert end == SimEnd(reason=EndReason.HANDOFF)


def test_ws2_patience_tolerance_follows_traits_with_a_seeded_jitter() -> None:
    for patience, low in ((0.0, 1), (0.4, 3), (1.0, 6)):
        tolerances = {
            Patience.for_persona(Traits(patience=patience), seed).tolerance for seed in range(64)
        }
        assert tolerances == {low, low + 1}
    assert Patience.for_persona(Traits(), 5) == Patience.for_persona(Traits(), 5)


def test_ws2_patience_progress_resets_the_streak() -> None:
    patience = Patience(tolerance=2)
    patience.record(progress=False)
    patience.record(progress=True)
    patience.record(progress=False)
    assert (patience.left, patience.exhausted) == (1, False)
    patience.record(progress=False)
    assert patience.exhausted


@pytest.mark.anyio
async def test_ws2_guided_abandons_when_patience_runs_out_even_if_the_llm_keeps_talking(
    make_brief: BriefFactory, say: DialogueFactory
) -> None:
    brief = make_brief(persona={"traits": {"patience": 0.0}})
    tolerance = Patience.for_persona(brief.persona.traits, brief.seed).tolerance
    llm = FakeLLM(responder=lambda _: decision_reply("Can you check again?", progress=False))
    simulator = make_simulator("fake/sim")(brief, llm)
    dialogue = say("user: Hi")
    moves: list[SimTurn | SimEnd] = []
    for _ in range(tolerance):
        dialogue += say("agent: Sorry, I don't understand.")
        moves.append(await simulator.next_turn(dialogue))
        dialogue += say("user: Can you check again?")

    assert all(isinstance(move, SimTurn) for move in moves[:-1])
    assert moves[-1] == SimEnd(reason=EndReason.GOAL_ABANDONED, closing_text="Can you check again?")
    assert "about to give up" in llm.calls[-1].messages[-1].content


@pytest.mark.anyio
async def test_ws2_guided_opening_message_never_counts_against_patience(
    make_brief: BriefFactory, say: DialogueFactory
) -> None:
    brief = make_brief(persona={"traits": {"patience": 0.0}})
    llm = FakeLLM(responder=lambda _: decision_reply("Hi, I need help.", progress=False))
    simulator = make_simulator("fake/sim")(brief, llm)
    assert isinstance(simulator, GuidedSimulator)

    await simulator.next_turn([])
    await simulator.next_turn(say("agent: Welcome to Acme!"))

    assert simulator.patience.strikes == 0


@pytest.mark.anyio
async def test_ws2_guided_stops_at_max_turns_without_calling_the_llm(
    make_brief: BriefFactory, say: DialogueFactory
) -> None:
    llm = FakeLLM()
    simulator = make_simulator("fake/sim")(make_brief(scenario={"limits": {"max_turns": 3}}), llm)

    end = await simulator.next_turn(say("user: a", "agent: b", "user: c"))

    assert end == SimEnd(reason=EndReason.MAX_TURNS)
    assert llm.calls == []


@pytest.mark.anyio
async def test_ws2_guided_stops_at_max_duration(make_brief: BriefFactory) -> None:
    clock = FakeClock()
    brief = make_brief(scenario={"limits": {"max_duration_s": 2}})
    simulator = make_simulator("fake/sim", clock_factory=lambda: clock)(brief, FakeLLM())

    clock.advance(2_000)

    assert await simulator.next_turn([]) == SimEnd(reason=EndReason.MAX_DURATION)


@pytest.mark.anyio
async def test_ws2_guided_email_subject_from_the_llm_or_the_scenario_title(
    make_brief: BriefFactory, say: DialogueFactory
) -> None:
    brief = make_brief(channel=Channel.EMAIL)
    with_subject = make_simulator("fake/sim")(
        brief, FakeLLM(script=[decision_reply("Hello, ...", subject="Moving my visit")])
    )
    without_subject = make_simulator("fake/sim")(brief, FakeLLM(script=[decision_reply("Hi")]))
    reply = make_simulator("fake/sim")(
        brief, FakeLLM(script=[decision_reply("Thanks", subject="ignored")])
    )

    assert await with_subject.next_turn([]) == SimTurn(text="Hello, ...", subject="Moving my visit")
    assert await without_subject.next_turn([]) == SimTurn(text="Hi", subject=brief.scenario.title)
    assert await reply.next_turn(say("user: Hi", "agent: Hello")) == SimTurn(text="Thanks")


@pytest.mark.anyio
async def test_ws2_replay_mode_runs_as_guided(make_brief: BriefFactory) -> None:
    brief = make_brief(
        scenario={"mode": "replay", "source": {"kind": "replay", "conversation_id": "prod-1"}}
    )
    llm = FakeLLM(script=[decision_reply("Hi, I need to move my visit.")])
    simulator = make_simulator("fake/sim")(brief, llm)

    assert isinstance(simulator, GuidedSimulator)
    assert await simulator.next_turn([]) == SimTurn(text="Hi, I need to move my visit.")
    assert "Move Thursday's visit" in (llm.calls[0].system or "")


@pytest.mark.anyio
async def test_ws2_guided_retries_malformed_json_without_counting_a_contradiction(
    make_brief: BriefFactory,
) -> None:
    llm = FakeLLM(script=["this is not json", decision_reply("Hi there.")])
    simulator = make_simulator("fake/sim")(make_brief(seed=3), llm)

    assert await simulator.next_turn([]) == SimTurn(text="Hi there.")
    assert simulator.contradictions == 0
    assert [call.seed for call in llm.calls] == [derive_subseed(3, "turn", 0, a) for a in (0, 1)]
    assert "not a valid JSON object" in llm.calls[1].messages[-1].content


@pytest.mark.anyio
async def test_ws2_guided_raises_simulator_error_when_no_attempt_is_usable(
    make_brief: BriefFactory,
) -> None:
    llm = FakeLLM(responder=lambda _: "nope")
    simulator = make_simulator("fake/sim", max_regenerations=1)(make_brief(), llm)

    with pytest.raises(SimulatorError, match="no valid draft in 2 attempt"):
        await simulator.next_turn([])
    assert len(llm.calls) == 2


def test_ws2_settings_reject_negative_regenerations() -> None:
    with pytest.raises(ValueError, match="max_regenerations"):
        make_simulator("fake/sim", max_regenerations=-1)


@pytest.mark.anyio
@pytest.mark.parametrize("factory", [make_simulator, make_adversarial_simulator])
async def test_ws2_default_output_limit_leaves_room_for_reasoning(
    make_brief: BriefFactory, factory: Callable[[str], SimulatorFactory]
) -> None:
    # A reasoning model counts its reasoning against the output limit. At 512 tokens,
    # gpt-6-luna cut off 4 opening probe turns of M1 run 01M5K3WE (a provider 400 or truncated
    # JSON), and each became a harness error.
    llm = FakeLLM(script=[decision_reply("Hi")])

    await factory("fake/sim")(make_brief(), llm).next_turn([])

    assert llm.calls[0].max_tokens >= DEFAULT_MAX_TOKENS >= 2048
