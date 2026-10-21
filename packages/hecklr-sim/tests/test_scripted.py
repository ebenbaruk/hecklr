"""Scripted mode: fixed user turns, exact reproduction, no model calls."""

from collections.abc import Callable

import pytest

from hecklr_llm import FakeLLM
from hecklr_schemas import (
    Channel,
    CustomerSimulator,
    EndReason,
    FakeClock,
    SimEnd,
    SimTurn,
    TrialBrief,
)
from hecklr_schemas.trial import DialogueTurn
from hecklr_sim import ScriptedSimulator, make_simulator

BriefFactory = Callable[..., TrialBrief]
DialogueFactory = Callable[..., list[DialogueTurn]]

SCRIPTED = {
    "mode": "scripted",
    "user_turns": ["Hi, I need to move my visit.", "C-1042", "Next week, mornings."],
    "limits": {"max_turns": 10, "max_duration_s": 60},
}


@pytest.mark.anyio
async def test_ws2_scripted_sends_user_turns_in_order_without_any_llm_call(
    make_brief: BriefFactory, say: DialogueFactory
) -> None:
    llm = FakeLLM()
    simulator = make_simulator("fake/sim")(make_brief(scenario=SCRIPTED), llm)

    first = await simulator.next_turn([])
    second = await simulator.next_turn(say("user: Hi, I need to move my visit.", "agent: ID?"))
    third = await simulator.next_turn(
        say("agent: Hello", "user: x", "agent: ID?", "user: C-1042", "agent: When?")
    )

    assert isinstance(simulator, ScriptedSimulator)
    assert isinstance(simulator, CustomerSimulator)
    assert [first, second, third] == [
        SimTurn(text="Hi, I need to move my visit."),
        SimTurn(text="C-1042"),
        SimTurn(text="Next week, mornings."),
    ]
    assert llm.calls == []
    assert simulator.contradictions == 0


@pytest.mark.anyio
async def test_ws2_scripted_position_comes_from_the_dialogue_so_replays_are_exact(
    make_brief: BriefFactory, say: DialogueFactory
) -> None:
    brief = make_brief(scenario=SCRIPTED)
    dialogue = say("user: a", "agent: b")

    answers = [await make_simulator("fake/sim")(brief, FakeLLM()).next_turn(dialogue) for _ in "xy"]

    assert answers == [SimTurn(text="C-1042")] * 2


@pytest.mark.anyio
async def test_ws2_scripted_ends_with_goal_achieved_when_the_script_runs_out(
    make_brief: BriefFactory, say: DialogueFactory
) -> None:
    simulator = make_simulator("fake/sim")(make_brief(scenario=SCRIPTED), FakeLLM())

    end = await simulator.next_turn(say("user: a", "agent: b", "user: c", "agent: d", "user: e"))

    assert end == SimEnd(reason=EndReason.GOAL_ACHIEVED)


@pytest.mark.anyio
async def test_ws2_scripted_stops_at_max_turns(
    make_brief: BriefFactory, say: DialogueFactory
) -> None:
    brief = make_brief(scenario={**SCRIPTED, "limits": {"max_turns": 2}})
    simulator = make_simulator("fake/sim")(brief, FakeLLM())

    end = await simulator.next_turn(say("user: a", "agent: b"))

    assert end == SimEnd(reason=EndReason.MAX_TURNS)


@pytest.mark.anyio
async def test_ws2_scripted_stops_at_max_duration_on_the_harness_clock(
    make_brief: BriefFactory,
) -> None:
    clock = FakeClock(start_ms=500)
    simulator = make_simulator("fake/sim", clock_factory=lambda: clock)(
        make_brief(scenario=SCRIPTED), FakeLLM()
    )

    clock.advance(59_999)
    assert isinstance(await simulator.next_turn([]), SimTurn)
    clock.advance(1)
    assert await simulator.next_turn([]) == SimEnd(reason=EndReason.MAX_DURATION)


@pytest.mark.anyio
async def test_ws2_scripted_email_first_message_carries_the_scenario_title_as_subject(
    make_brief: BriefFactory, say: DialogueFactory
) -> None:
    brief = make_brief(scenario=SCRIPTED, channel=Channel.EMAIL)
    simulator = make_simulator("fake/sim")(brief, FakeLLM())

    first = await simulator.next_turn([])
    second = await simulator.next_turn(say("user: a", "agent: b"))

    assert first == SimTurn(text="Hi, I need to move my visit.", subject=brief.scenario.title)
    assert isinstance(second, SimTurn)
    assert second.subject is None


def test_ws2_scripted_simulator_refuses_a_brief_without_user_turns(
    make_brief: BriefFactory,
) -> None:
    with pytest.raises(ValueError, match="no user_turns"):
        ScriptedSimulator(make_brief(), FakeClock())
