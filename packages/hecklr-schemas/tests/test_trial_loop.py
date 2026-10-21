"""Contract-shape tests for the trial-loop Protocols: a minimal loop must type-check and run."""

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from pydantic import ValidationError

from hecklr_schemas import (
    AgentChunk,
    Channel,
    ChannelDriver,
    ChatSession,
    ChatTarget,
    CustomerSimulator,
    DialogueTurn,
    DriverResult,
    EndReason,
    Event,
    EventId,
    EventPayload,
    EventSink,
    FakeClock,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Persona,
    Scenario,
    SessionContext,
    SessionSummary,
    SimEnd,
    SimTurn,
    SimulatorFactory,
    ToolCallPayload,
    ToolResultPayload,
    TrialBrief,
    TrialId,
    UtterancePayload,
    World,
    WorldSandbox,
    WorldSession,
    WorldSnapshot,
    new_id,
)
from hecklr_schemas.common import Actor

# --- minimal implementations ----------------------------------------------------------------


class ListSink:
    def __init__(self, trial_id: TrialId, clock: FakeClock) -> None:
        self.trial_id = trial_id
        self.clock = clock
        self.events: list[Event] = []

    def now_ms(self) -> int:
        return self.clock.now_ms()

    def emit(self, actor: Actor, payload: EventPayload, *, t_ms: int | None = None) -> Event:
        event = Event(
            id=EventId(new_id()),
            trial_id=self.trial_id,
            seq=len(self.events),
            t_ms=self.now_ms() if t_ms is None else t_ms,
            actor=actor,
            payload=payload,
        )
        self.events.append(event)
        return event


class ScriptedCustomer:
    def __init__(self, lines: Sequence[str]) -> None:
        self._lines = list(lines)

    @property
    def contradictions(self) -> int:
        return 0

    async def next_turn(self, dialogue: Sequence[DialogueTurn]) -> SimTurn | SimEnd:
        said = sum(turn.speaker == "user" for turn in dialogue)
        if said == len(self._lines):
            return SimEnd(reason=EndReason.GOAL_ACHIEVED, closing_text="thanks")
        return SimTurn(text=self._lines[said])


def scripted_factory(brief: TrialBrief, llm: LLMProvider) -> CustomerSimulator:
    return ScriptedCustomer(brief.scenario.user_turns or [])


class DictWorldSession:
    def __init__(self, world: World, sink: EventSink) -> None:
        self._world = world
        self._sink = sink
        self.closed = False

    @property
    def mcp_url(self) -> str | None:
        return "http://127.0.0.1:9100/mcp"

    @property
    def http_url(self) -> str | None:
        return None

    def call(self, tool: str) -> None:
        self._sink.emit("agent", ToolCallPayload(call_id="c1", tool=tool))
        self._sink.emit("tool", ToolResultPayload(call_id="c1", tool=tool, latency_ms=0))

    def snapshot(self) -> WorldSnapshot:
        records = {
            name: {str(rec[col.key]): rec for rec in col.records}
            for name, col in self._world.collections.items()
        }
        return WorldSnapshot(
            world_id=self._world.id, taken_at_ms=self._sink.now_ms(), collections=records
        )

    async def close(self) -> None:
        self.closed = True


class DictSandbox:
    def __init__(self) -> None:
        self.sessions: list[DictWorldSession] = []

    async def open(self, brief: TrialBrief, world: World, sink: EventSink) -> WorldSession:
        session = DictWorldSession(world, sink)
        self.sessions.append(session)
        return session


class EchoSession:
    async def send(self, text: str) -> AsyncIterator[AgentChunk]:
        yield AgentChunk(text=f"you said: {text}", final=True)

    async def close(self) -> SessionSummary:
        return SessionSummary()


class EchoTarget:
    async def open(self, ctx: SessionContext) -> ChatSession:
        return EchoSession()


class ChatDriver:
    def __init__(self, target: ChatTarget, clock: FakeClock) -> None:
        self._target = target
        self._clock = clock

    @property
    def channel(self) -> Channel:
        return Channel.CHAT

    async def run(
        self,
        ctx: SessionContext,
        brief: TrialBrief,
        simulator: CustomerSimulator,
        sink: EventSink,
    ) -> DriverResult:
        session = await self._target.open(ctx)
        dialogue: list[DialogueTurn] = []
        while True:
            move = await simulator.next_turn(dialogue)
            if isinstance(move, SimEnd):
                return DriverResult(
                    status="ok",
                    end_reason=move.reason,
                    turns=len(dialogue),
                    summary=await session.close(),
                )
            sent_ms = sink.now_ms()
            sink.emit("user", UtterancePayload(text=move.text, turn=len(dialogue), end_ms=sent_ms))
            dialogue.append(DialogueTurn(speaker="user", text=move.text))
            self._clock.advance(120)
            first_ms = sink.now_ms()
            reply = "".join([chunk.text async for chunk in session.send(move.text)])
            self._clock.advance(80)
            sink.emit(
                "agent",
                UtterancePayload(
                    text=reply,
                    turn=len(dialogue),
                    end_ms=sink.now_ms(),
                    latency_ms=first_ms - sent_ms,
                ),
                t_ms=first_ms,
            )
            dialogue.append(DialogueTurn(speaker="agent", text=reply))


class NullLLM:
    async def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(model=request.model, text="")


# --- tests ----------------------------------------------------------------------------------


def _brief(load_yaml: Any) -> TrialBrief:
    scenario = Scenario.model_validate(
        {
            **load_yaml("spec_scenario.yaml"),
            "mode": "scripted",
            "user_turns": ["move my visit", "tuesday works"],
        }
    )
    return TrialBrief(
        trial_id=TrialId(new_id()),
        scenario=scenario,
        persona=Persona.model_validate(load_yaml("spec_persona.yaml")),
        channel=Channel.CHAT,
        seed=11,
    )


def test_minimal_implementations_satisfy_the_protocols() -> None:
    clock = FakeClock()
    assert isinstance(ListSink(TrialId(new_id()), clock), EventSink)
    assert isinstance(ScriptedCustomer([]), CustomerSimulator)
    assert isinstance(scripted_factory, SimulatorFactory)
    assert isinstance(DictSandbox(), WorldSandbox)
    assert isinstance(ChatDriver(EchoTarget(), clock), ChannelDriver)


@pytest.mark.anyio
async def test_one_trial_runs_through_the_loop_with_a_single_event_log(load_yaml: Any) -> None:
    brief = _brief(load_yaml)
    world_def = World.model_validate(load_yaml("acme_world.yaml"))
    clock = FakeClock()
    sink = ListSink(brief.trial_id, clock)
    sandbox = DictSandbox()
    factory: SimulatorFactory = scripted_factory

    world = await sandbox.open(brief, world_def, sink)
    before = world.snapshot()
    ctx = SessionContext(
        trial_id=brief.trial_id,
        seed=brief.seed,
        world_mcp_url=world.mcp_url,
        world_http_url=world.http_url,
    )
    sandbox.sessions[0].call("get_customer")
    driver: ChannelDriver = ChatDriver(EchoTarget(), clock)
    result = await driver.run(ctx, brief, factory(brief, NullLLM()), sink)
    after = world.snapshot()
    await world.close()

    assert result.status == "ok"
    assert result.end_reason is EndReason.GOAL_ACHIEVED
    assert result.turns == 4
    assert [e.seq for e in sink.events] == list(range(len(sink.events)))
    assert [e.kind for e in sink.events[:2]] == ["tool_call", "tool_result"]
    agent_turns = [e for e in sink.events if e.actor == "agent" and e.kind == "utterance"]
    assert len(agent_turns) == 2
    first_reply = agent_turns[0].payload
    assert isinstance(first_reply, UtterancePayload)
    # Time to first token and to last token, as the chat channel defines them.
    assert first_reply.latency_ms == 120
    assert first_reply.end_ms is not None
    assert first_reply.latency_ms + first_reply.end_ms - agent_turns[0].t_ms == 200
    assert (before.taken_at_ms, after.taken_at_ms) == (0, 400)
    assert sandbox.sessions[0].closed


def test_sim_turn_needs_text_and_driver_result_needs_a_known_status() -> None:
    with pytest.raises(ValidationError):
        SimTurn(text="")
    with pytest.raises(ValidationError):
        DriverResult.model_validate({"status": "harness_error", "end_reason": "error", "turns": 0})
