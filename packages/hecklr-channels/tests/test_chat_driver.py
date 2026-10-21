"""ChatDriver behaviour on fake clocks and scripted agents: timing, events, limits and failures."""

from collections.abc import Sequence

import anyio
import pytest

from hecklr_adapters import AgentError, AgentTimeoutError
from hecklr_channels import ChatDriver
from hecklr_llm import FakeLLM
from hecklr_schemas import (
    AgentChunk,
    Channel,
    ChannelDriver,
    DialogueTurn,
    EndReason,
    ErrorPayload,
    FakeClock,
    HandoffPayload,
    MonotonicClock,
    SimEnd,
    SimTurn,
    ToolCallPayload,
    ToolResultPayload,
)

from .fakes import ListSink, LLMCustomer, ScriptedAgent, ScriptedCustomer, Step, brief, context

pytestmark = pytest.mark.anyio

STALL = Step(sleep_s=30)


def _errors(sink: ListSink) -> list[str]:
    return [e.payload.message for e in sink.events if isinstance(e.payload, ErrorPayload)]


def test_ws6_chat_driver_satisfies_the_channel_driver_protocol() -> None:
    driver = ChatDriver(ScriptedAgent(FakeClock(), []))
    assert isinstance(driver, ChannelDriver)
    assert driver.channel is Channel.CHAT


async def test_ws6_chat_timing_ttft_and_ttlt_follow_adr_0006() -> None:
    clock = FakeClock(start_ms=1_000)
    sink = ListSink(clock)
    agent = ScriptedAgent(
        clock,
        [
            [
                Step(AgentChunk(text="Hel"), advance_ms=120),
                Step(AgentChunk(text="lo"), advance_ms=30),
                Step(AgentChunk(final=True), advance_ms=50),
            ]
        ],
    )
    result = await ChatDriver(agent).run(context(), brief(), ScriptedCustomer(["hi"]), sink)

    assert (result.status, result.end_reason, result.turns) == ("ok", EndReason.GOAL_ACHIEVED, 2)
    [(user_event, user)] = sink.utterances("user")
    [(agent_event, reply)] = sink.utterances("agent")
    assert (user_event.t_ms, user.end_ms, user.latency_ms, user.turn) == (1_000, 1_000, None, 0)
    assert (reply.text, reply.turn) == ("Hello", 1)
    assert agent_event.t_ms == 1_120  # first token
    assert reply.end_ms == 1_150  # last token, not the empty final chunk
    assert reply.latency_ms == 120  # time to first token
    assert reply.latency_ms + reply.end_ms - agent_event.t_ms == 150  # time to last token
    assert result.summary is not None
    assert result.summary.agent_session_id == "agent-session"
    assert agent.closed == 1


async def test_ws6_chat_streamed_tool_events_are_logged_as_they_arrive() -> None:
    clock = FakeClock()
    sink = ListSink(clock)
    call = ToolCallPayload(call_id="c1", tool="find_slot")
    result_payload = ToolResultPayload(call_id="c1", tool="find_slot", result=[], latency_ms=40)
    agent = ScriptedAgent(
        clock,
        [
            [
                Step(AgentChunk(tool_call=call), advance_ms=10),
                Step(AgentChunk(tool_result=result_payload), advance_ms=40),
                Step(AgentChunk(text="No slots.", final=True), advance_ms=5),
            ]
        ],
    )
    await ChatDriver(agent).run(context(), brief(), ScriptedCustomer(["any slot?"]), sink)

    assert sink.kinds() == [
        "user:utterance",
        "agent:tool_call",
        "tool:tool_result",
        "agent:utterance",
    ]
    assert [e.t_ms for e in sink.events] == [0, 10, 50, 55]
    assert sink.events[1].payload == call


async def test_ws6_chat_handoff_chunk_ends_the_conversation_with_handoff() -> None:
    clock = FakeClock()
    sink = ListSink(clock)
    handoff = HandoffPayload(reason="gas smell", target="emergency desk")
    agent = ScriptedAgent(
        clock, [[Step(AgentChunk(text="Transferring you.", handoff=handoff, final=True))]]
    )
    customer = ScriptedCustomer(["I smell gas", "hello?"])
    result = await ChatDriver(agent).run(context(), brief(), customer, sink)

    assert (result.status, result.end_reason, result.turns) == ("ok", EndReason.HANDOFF, 2)
    assert sink.kinds() == ["user:utterance", "agent:handoff", "agent:utterance"]
    assert agent.received == ["I smell gas"]


async def test_ws6_chat_agent_closing_the_conversation_ends_it() -> None:
    clock = FakeClock()
    agent = ScriptedAgent(clock, [[Step(AgentChunk(text="Bye.", ended=True, final=True))]])
    result = await ChatDriver(agent).run(
        context(), brief(), ScriptedCustomer(["thanks", "wait"]), ListSink(clock)
    )
    assert (result.status, result.end_reason) == ("ok", EndReason.AGENT_ENDED)


async def test_ws6_chat_sim_end_closing_text_is_sent_and_answered() -> None:
    clock = FakeClock()
    sink = ListSink(clock)
    end = SimEnd(reason=EndReason.GOAL_ABANDONED, closing_text="forget it, bye")
    agent = ScriptedAgent(clock, [])
    result = await ChatDriver(agent).run(context(), brief(), ScriptedCustomer(["hi"], end), sink)

    assert (result.status, result.end_reason, result.turns) == ("ok", EndReason.GOAL_ABANDONED, 4)
    assert agent.received == ["hi", "forget it, bye"]
    assert [u.text for _, u in sink.utterances("user")] == ["hi", "forget it, bye"]


async def test_ws6_chat_closing_text_is_dropped_when_time_ran_out() -> None:
    clock = FakeClock()

    class SlowToLeave(ScriptedCustomer):
        async def next_turn(self, dialogue: Sequence[DialogueTurn]) -> SimTurn | SimEnd:
            move = await super().next_turn(dialogue)
            if isinstance(move, SimEnd):
                clock.advance(2_000)  # the customer's model took its time deciding to leave
            return move

    end = SimEnd(reason=EndReason.GOAL_ACHIEVED, closing_text="bye")
    agent = ScriptedAgent(clock, [])
    result = await ChatDriver(agent).run(
        context(), brief(max_duration_s=1), SlowToLeave(["hi"], end), ListSink(clock)
    )
    assert (result.status, result.end_reason, result.turns) == ("ok", EndReason.GOAL_ACHIEVED, 2)
    assert agent.received == ["hi"]


async def test_ws6_chat_failure_answering_the_closing_text_is_reported() -> None:
    clock = FakeClock()
    end = SimEnd(reason=EndReason.GOAL_ACHIEVED, closing_text="bye")
    agent = ScriptedAgent(clock, [[], [Step(raise_error=AgentError("crashed on goodbye"))]])
    result = await ChatDriver(agent).run(
        context(), brief(), ScriptedCustomer(["hi"], end), ListSink(clock)
    )
    assert (result.status, result.end_reason) == ("agent_error", EndReason.ERROR)


@pytest.mark.parametrize(("max_turns", "turns"), [(5, 4), (6, 6), (1, 0)])
async def test_ws6_chat_max_turns_counts_both_parties_and_never_leaves_a_message_unanswered(
    max_turns: int, turns: int
) -> None:
    clock = FakeClock()
    customer = ScriptedCustomer(["more"] * 50)
    result = await ChatDriver(ScriptedAgent(clock, [])).run(
        context(), brief(max_turns=max_turns), customer, ListSink(clock)
    )
    assert (result.status, result.end_reason, result.turns) == ("ok", EndReason.MAX_TURNS, turns)
    assert len(customer.seen) == turns // 2  # never asked for a turn it could not take


async def test_ws6_chat_max_duration_between_turns_ends_normally() -> None:
    clock = FakeClock()
    slow_reply = [Step(AgentChunk(text="ok", final=True), advance_ms=400)]
    agent = ScriptedAgent(clock, [], default=slow_reply)
    result = await ChatDriver(agent).run(
        context(), brief(max_duration_s=1), ScriptedCustomer(["more"] * 50), ListSink(clock)
    )
    assert (result.status, result.end_reason, result.turns) == ("ok", EndReason.MAX_DURATION, 6)


async def test_ws6_chat_max_duration_running_out_mid_reply_is_a_timeout() -> None:
    sink = ListSink(MonotonicClock())
    agent = ScriptedAgent(FakeClock(), [[Step(AgentChunk(text="thinking")), STALL]])
    with anyio.fail_after(5):
        result = await ChatDriver(agent).run(
            context(timeout_s=60), brief(max_duration_s=1), ScriptedCustomer(["hi"]), sink
        )
    assert (result.status, result.end_reason, result.turns) == (
        "timeout",
        EndReason.MAX_DURATION,
        2,
    )
    assert result.error is not None
    assert "max_duration_s=1" in result.error
    assert [u.text for _, u in sink.utterances("agent")] == ["thinking"]  # partial reply kept
    assert agent.finalized == 1  # the stalled stream was closed
    assert agent.closed == 1


async def test_ws6_chat_per_turn_deadline_is_a_timeout_with_an_error_event() -> None:
    sink = ListSink(MonotonicClock())
    agent = ScriptedAgent(FakeClock(), [[STALL]])
    result = await ChatDriver(agent).run(
        context(timeout_s=0.1), brief(), ScriptedCustomer(["hi"]), sink
    )

    assert (result.status, result.end_reason, result.turns) == ("timeout", EndReason.ERROR, 1)
    assert _errors(sink) == ["agent did not finish its reply within 0.1 s"]
    assert sink.utterances("agent") == []
    assert sink.events[-1].actor == "system"


async def test_ws6_chat_adapter_timeout_is_a_timeout() -> None:
    clock = FakeClock()
    agent = ScriptedAgent(clock, [[Step(raise_error=AgentTimeoutError("socket idle"))]])
    result = await ChatDriver(agent).run(
        context(), brief(), ScriptedCustomer(["hi"]), ListSink(clock)
    )
    assert (result.status, result.end_reason, result.error) == (
        "timeout",
        EndReason.ERROR,
        "socket idle",
    )


async def test_ws6_chat_agent_error_mid_stream_keeps_the_partial_reply() -> None:
    clock = FakeClock()
    sink = ListSink(clock)
    agent = ScriptedAgent(
        clock,
        [
            [
                Step(AgentChunk(text="Sure, "), advance_ms=20),
                Step(raise_error=AgentError("HTTP 500")),
            ]
        ],
    )
    result = await ChatDriver(agent).run(context(), brief(), ScriptedCustomer(["hi"]), sink)

    assert (result.status, result.end_reason, result.error) == (
        "agent_error",
        EndReason.ERROR,
        "HTTP 500",
    )
    assert [u.text for _, u in sink.utterances("agent")] == ["Sure, "]
    assert _errors(sink) == ["HTTP 500"]
    assert agent.closed == 1


async def test_ws6_chat_session_that_cannot_open_is_an_agent_error() -> None:
    clock = FakeClock()
    agent = ScriptedAgent(clock, [], open_error=AgentError("connection refused"))
    sink = ListSink(clock)
    result = await ChatDriver(agent).run(context(), brief(), ScriptedCustomer(["hi"]), sink)
    assert (result.status, result.turns, result.summary) == ("agent_error", 0, None)
    assert _errors(sink) == ["connection refused"]


async def test_ws6_chat_session_that_never_opens_is_a_timeout() -> None:
    agent = ScriptedAgent(FakeClock(), [], open_sleep_s=30)
    result = await ChatDriver(agent).run(
        context(timeout_s=0.05), brief(), ScriptedCustomer(["hi"]), ListSink(MonotonicClock())
    )
    assert (result.status, result.end_reason) == ("timeout", EndReason.ERROR)


async def test_ws6_chat_close_failures_are_logged_without_failing_the_trial() -> None:
    clock = FakeClock()
    sink = ListSink(clock)
    agent = ScriptedAgent(clock, [], close_error=AgentError("already gone"))
    result = await ChatDriver(agent).run(context(), brief(), ScriptedCustomer(["hi"]), sink)
    assert (result.status, result.summary) == ("ok", None)
    assert _errors(sink) == ["closing the session failed: already gone"]

    hung = ScriptedAgent(clock, [], close_sleep_s=30)
    result = await ChatDriver(hung).run(
        context(timeout_s=0.05), brief(), ScriptedCustomer([]), sink
    )
    assert (result.status, result.summary) == ("ok", None)
    assert _errors(sink)[-1] == "agent did not close the session within 0.05 s"


async def test_ws6_chat_empty_reply_is_logged_without_a_first_token() -> None:
    clock = FakeClock()
    sink = ListSink(clock)
    agent = ScriptedAgent(clock, [[Step(AgentChunk(final=True), advance_ms=70)]])
    await ChatDriver(agent).run(context(), brief(), ScriptedCustomer(["hello?"]), sink)
    [(event, reply)] = sink.utterances("agent")
    assert (reply.text, event.t_ms, reply.end_ms, reply.latency_ms) == ("", 70, 70, None)


async def test_ws6_chat_stream_ending_without_a_final_chunk_ends_the_turn() -> None:
    clock = FakeClock()
    sink = ListSink(clock)
    agent = ScriptedAgent(clock, [[Step(AgentChunk(text="partial but done"))]])
    result = await ChatDriver(agent).run(context(), brief(), ScriptedCustomer(["hi"]), sink)
    assert result.status == "ok"
    assert [u.text for _, u in sink.utterances("agent")] == ["partial but done"]


async def test_ws6_chat_stops_reading_at_the_final_chunk_and_closes_the_stream() -> None:
    clock = FakeClock()
    sink = ListSink(clock)
    script = [Step(AgentChunk(text="done", final=True)), Step(AgentChunk(text="never read"))]
    agent = ScriptedAgent(clock, [script])
    await ChatDriver(agent).run(context(), brief(), ScriptedCustomer(["hi"]), sink)
    assert [u.text for _, u in sink.utterances("agent")] == ["done"]
    assert agent.finalized == 1


async def test_ws6_chat_simulator_failure_propagates_and_still_closes_the_session() -> None:
    class BrokenSimulator(ScriptedCustomer):
        async def next_turn(self, dialogue: object) -> SimEnd:
            raise RuntimeError("simulator LLM unavailable")

    clock = FakeClock()
    agent = ScriptedAgent(clock, [], close_error=AgentError("close failed too"))
    with pytest.raises(RuntimeError, match="simulator LLM unavailable") as caught:
        await ChatDriver(agent).run(context(), brief(), BrokenSimulator([]), ListSink(clock))
    assert agent.closed == 1
    assert caught.value.__notes__ == ["closing the agent session also failed: close failed too"]


async def test_ws6_chat_cancelled_trial_still_closes_the_session() -> None:
    agent = ScriptedAgent(FakeClock(), [[STALL]])
    with anyio.move_on_after(0.1):
        await ChatDriver(agent).run(
            context(), brief(), ScriptedCustomer(["hi"]), ListSink(MonotonicClock())
        )
    assert agent.closed == 1


async def test_ws6_chat_end_to_end_with_a_fake_llm_customer() -> None:
    clock = FakeClock()
    sink = ListSink(clock)
    llm = FakeLLM(script=["I need to move my visit", "Tuesday works", "[END]"])
    replies = [
        [Step(AgentChunk(text="Sure, which day?", final=True), advance_ms=300)],
        [Step(AgentChunk(text="Moved to Tuesday.", final=True), advance_ms=250)],
    ]
    agent = ScriptedAgent(clock, replies)
    result = await ChatDriver(agent).run(context(), brief(), LLMCustomer(llm), sink)

    assert (result.status, result.end_reason, result.turns) == ("ok", EndReason.GOAL_ACHIEVED, 4)
    assert [(e.actor, u.text) for e, u in sink.utterances("user") + sink.utterances("agent")] == [
        ("user", "I need to move my visit"),
        ("user", "Tuesday works"),
        ("agent", "Sure, which day?"),
        ("agent", "Moved to Tuesday."),
    ]
    # The simulator saw the agent's replies, and only the visible dialogue.
    assert llm.calls[-1].messages[0].content == (
        "user: I need to move my visit\nagent: Sure, which day?\n"
        "user: Tuesday works\nagent: Moved to Tuesday."
    )
    assert [e.seq for e in sink.events] == list(range(4))
    assert [u.latency_ms for _, u in sink.utterances("agent")] == [300, 250]
