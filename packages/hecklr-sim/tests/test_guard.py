"""Self-consistency guard: deterministic checks, the optional LLM check, regeneration, counts."""

from collections.abc import Callable

import pytest
from pydantic import BaseModel

from hecklr_llm import FakeLLM
from hecklr_schemas import DialogueTurn, LLMRequest, SimTurn, TrialBrief
from hecklr_sim import (
    ConsistencyGuard,
    GuidedSimulator,
    SimulatorError,
    derive_seed,
    make_simulator,
)
from hecklr_sim.decision import SimDecision
from hecklr_sim.testing import decision_reply, verdict_reply

BriefFactory = Callable[..., TrialBrief]
DialogueFactory = Callable[..., list[DialogueTurn]]


def _kinds(
    guard: ConsistencyGuard, text: str, dialogue: list[DialogueTurn] | None = None
) -> list[str]:
    return [issue.kind for issue in guard.check(decision_reply(text), dialogue or [])]


@pytest.fixture
def guard(make_brief: BriefFactory) -> ConsistencyGuard:
    return ConsistencyGuard(make_brief().scenario)


def test_ws2_guard_flags_a_wrong_customer_id(guard: ConsistencyGuard) -> None:
    issues = guard.check(decision_reply("Sure, it's C-1043."), [])

    assert [issue.kind for issue in issues] == ["wrong_fact_value"]
    assert "'C-1043'" in issues[0].detail
    assert "'C-1042'" in issues[0].detail


@pytest.mark.parametrize(
    "text",
    [
        "Sure, it's C-1042.",
        "it's c-1042, postcode 92000",
        "Thursday at 9 please, mornings only.",
        "My visit is on 2026-10-23T09:00.",
        "I'd like next week if possible.",
    ],
)
def test_ws2_guard_accepts_turns_consistent_with_the_brief(
    guard: ConsistencyGuard, text: str
) -> None:
    assert _kinds(guard, text) == []


def test_ws2_guard_flags_a_wrong_postcode(guard: ConsistencyGuard) -> None:
    assert _kinds(guard, "My postcode is 92100.") == ["wrong_fact_value"]


def test_ws2_guard_never_flags_values_the_agent_said(
    guard: ConsistencyGuard, say: DialogueFactory
) -> None:
    dialogue = say("user: Hi", "agent: Is your customer ID C-1043?")

    assert _kinds(guard, "No, not C-1043, it's C-1042.", dialogue) == []


@pytest.mark.parametrize("text", ["My order is ORD-5581.", "Call me on 0612345678."])
def test_ws2_guard_flags_invented_identifiers(guard: ConsistencyGuard, text: str) -> None:
    assert _kinds(guard, text) == ["invented_identifier"]


def test_ws2_guard_flags_facts_the_brief_does_not_have(guard: ConsistencyGuard) -> None:
    def kinds(revealed: list[str]) -> list[str]:
        draft = decision_reply("Here you go.", facts_revealed=revealed)
        return [issue.kind for issue in guard.check(draft, [])]

    assert kinds(["customer_id", "Postcode", "92000"]) == []
    assert kinds(["date_of_birth"]) == ["unknown_fact"]


def test_ws2_guard_flags_malformed_decisions(guard: ConsistencyGuard) -> None:
    empty_speak = decision_reply("   ")
    end_without_reason = SimDecision.model_validate(
        {**decision_reply("bye").model_dump(), "action": "end"}
    )

    assert [i.kind for i in guard.check(empty_speak, [])] == ["malformed"]
    assert [i.kind for i in guard.check(end_without_reason, [])] == ["malformed"]


def test_ws2_guard_handles_nested_and_free_text_facts(make_brief: BriefFactory) -> None:
    brief = make_brief(
        scenario={
            "facts": {"name": "Jo Smith", "orders": ["ORD-1", {"ref": "ORD-77123"}], "vip": True}
        }
    )
    guard = ConsistencyGuard(brief.scenario)

    assert _kinds(guard, "I'm Jo Smith, order ORD-77123.") == []
    assert _kinds(guard, "Order ORD-99999.") == ["invented_identifier"]


@pytest.mark.anyio
async def test_ws2_guard_regenerates_a_wrong_id_and_counts_the_contradiction(
    make_brief: BriefFactory, say: DialogueFactory
) -> None:
    llm = FakeLLM(
        script=[
            decision_reply("It's C-1043.", facts_revealed=["customer_id"]),
            decision_reply("It's C-1042.", facts_revealed=["customer_id"]),
        ]
    )
    simulator = make_simulator("fake/sim")(make_brief(seed=5), llm)
    assert isinstance(simulator, GuidedSimulator)

    turn = await simulator.next_turn(say("user: Hi", "agent: What's your customer ID?"))

    assert turn == SimTurn(text="It's C-1042.")
    assert simulator.contradictions == 1
    assert [issue.kind for issue in simulator.issues] == ["wrong_fact_value"]
    retry = llm.calls[1]
    assert retry.seed == derive_seed(5, "turn", 1, 1) != llm.calls[0].seed
    assert "previous draft was rejected" in retry.messages[-1].content
    assert "'C-1043'" in retry.messages[-1].content


@pytest.mark.anyio
async def test_ws2_guard_counts_one_contradiction_per_regenerated_turn(
    make_brief: BriefFactory, say: DialogueFactory
) -> None:
    llm = FakeLLM(
        script=[
            decision_reply("Hi, ID C-9999."),
            decision_reply("Hi, ID C-8888."),
            decision_reply("Hi, ID C-1042."),
            decision_reply("Postcode 92001."),
            decision_reply("Postcode 92000."),
            decision_reply("Next week, mornings."),
        ]
    )
    simulator = make_simulator("fake/sim")(make_brief(), llm)

    await simulator.next_turn([])
    await simulator.next_turn(say("user: Hi, ID C-1042.", "agent: Postcode?"))
    await simulator.next_turn(say("user: a", "agent: b", "user: Postcode 92000.", "agent: When?"))

    assert simulator.contradictions == 2
    assert len(llm.calls) == 6


@pytest.mark.anyio
async def test_ws2_guard_llm_check_catches_a_changed_goal(
    make_brief: BriefFactory, say: DialogueFactory
) -> None:
    drafts = iter(
        [
            decision_reply("Actually I want to cancel my whole contract."),
            decision_reply("Can we move it to next week, mornings?"),
        ]
    )

    def respond(request: LLMRequest) -> BaseModel:
        if request.schema_name == "SimDecision":
            return next(drafts)
        draft = request.messages[-1].content.split("## Draft to check\n")[1]
        changed = "cancel" in draft
        return verdict_reply(
            consistent=not changed, issue="the goal changed to a cancellation" if changed else ""
        )

    llm = FakeLLM(responder=respond)
    simulator = make_simulator("fake/sim", llm_consistency_check=True)(make_brief(seed=9), llm)
    assert isinstance(simulator, GuidedSimulator)

    turn = await simulator.next_turn(say("user: Hi", "agent: How can I help?"))

    assert turn == SimTurn(text="Can we move it to next week, mornings?")
    assert simulator.contradictions == 1
    assert [issue.kind for issue in simulator.issues] == ["llm_flagged"]
    checks = [call for call in llm.calls if call.schema_name == "ConsistencyVerdict"]
    assert [call.seed for call in checks] == [derive_seed(9, "check", 1, a) for a in (0, 1)]
    assert "Move Thursday's visit" in checks[0].messages[-1].content
    assert "the goal changed to a cancellation" in llm.calls[-2].messages[-1].content


@pytest.mark.anyio
async def test_ws2_guard_skips_the_llm_check_when_deterministic_checks_already_failed(
    make_brief: BriefFactory,
) -> None:
    llm = FakeLLM(
        rules=[("## Draft to check", verdict_reply(consistent=True))],
        script=[decision_reply("ID C-1043"), decision_reply("ID C-1042")],
    )
    simulator = make_simulator("fake/sim", llm_consistency_check=True)(make_brief(), llm)

    await simulator.next_turn([])

    assert [call.schema_name for call in llm.calls] == [
        "SimDecision",
        "SimDecision",
        "ConsistencyVerdict",
    ]


@pytest.mark.anyio
async def test_ws2_guard_gives_up_with_a_harness_error_after_bounded_retries(
    make_brief: BriefFactory,
) -> None:
    llm = FakeLLM(responder=lambda _: decision_reply("It's C-1043."))
    simulator = make_simulator("fake/sim", max_regenerations=2)(make_brief(), llm)

    with pytest.raises(SimulatorError, match=r"C-1043.*C-1042"):
        await simulator.next_turn([])
    assert len(llm.calls) == 3
