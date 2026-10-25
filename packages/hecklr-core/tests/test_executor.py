"""RunExecutor against fake simulators, worlds, drivers and graders (frozen Protocols)."""

import dataclasses
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import anyio
import pytest
import structlog
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer
from structlog.contextvars import merge_contextvars
from structlog.testing import capture_logs

from hecklr_core import (
    Collaborators,
    ResultStore,
    RetryPolicy,
    RunExecutor,
    Suite,
    TrialGrader,
    extend_matrix,
    is_retryable,
    load_project,
    plan_run,
    resolve_suite,
)
from hecklr_core.storage import RUN_JSON
from hecklr_llm import LLMProviderError, UnknownPriceError
from hecklr_schemas import (
    Budget,
    Channel,
    CustomerSimulator,
    DialogueTurn,
    DriverResult,
    EndReason,
    ErrorPayload,
    Event,
    EventSink,
    Grade,
    GradeContext,
    GradeId,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    Message,
    Run,
    RunStatus,
    ScenarioLimits,
    SessionContext,
    SimEnd,
    SimTurn,
    ToolCallPayload,
    ToolResultPayload,
    Trial,
    TrialBrief,
    UtterancePayload,
    World,
    WorldSnapshot,
    new_id,
)

NO_WAIT = RetryPolicy(base_delay_s=0.0, max_delay_s=0.0)


# --- fake trial-loop components ------------------------------------------------------------


class FakeWorldSession:
    def __init__(self, world: World, sink: EventSink) -> None:
        self._world = world
        self._sink = sink
        self.state = {
            name: {str(record[c.key]): dict(record) for record in c.records}
            for name, c in world.collections.items()
        }
        self.closed = False

    @property
    def mcp_url(self) -> str | None:
        return "http://world.test/mcp"

    @property
    def http_url(self) -> str | None:
        return "http://world.test/api"

    def snapshot(self) -> WorldSnapshot:
        return WorldSnapshot.model_validate(
            {
                "world_id": self._world.id,
                "taken_at_ms": self._sink.now_ms(),
                "collections": self.state,
            }
        )

    def call_tool(self, tool: str, customer_id: str, slot: str) -> None:
        self._sink.emit(
            "agent", ToolCallPayload(call_id="c1", tool=tool, args={"customer_id": customer_id})
        )
        self.state["appointments"][customer_id]["slot"] = slot
        self._sink.emit(
            "tool", ToolResultPayload(call_id="c1", tool=tool, result={"ok": True}, latency_ms=1)
        )

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeSandbox:
    """Opens a fresh in-memory world; ``fail_opens`` makes the first N opens raise."""

    fail_opens: int = 0
    error: Callable[[], Exception] = lambda: ConnectionError("world server down")
    sessions: list[FakeWorldSession] = field(default_factory=list)
    opens: int = 0

    async def open(self, brief: TrialBrief, world: World, sink: EventSink) -> FakeWorldSession:
        self.opens += 1
        if self.opens <= self.fail_opens:
            raise self.error()
        session = FakeWorldSession(world, sink)
        self.sessions.append(session)
        return session


class FakeSimulator:
    """Says a fixed number of things through the LLM, then ends with goal achieved."""

    def __init__(self, brief: TrialBrief, llm: LLMProvider, turns: int = 2) -> None:
        self._brief = brief
        self._llm = llm
        self._turns = turns

    @property
    def contradictions(self) -> int:
        return 1

    async def next_turn(self, dialogue: Sequence[DialogueTurn]) -> SimTurn | SimEnd:
        if sum(turn.speaker == "user" for turn in dialogue) >= self._turns:
            return SimEnd(reason=EndReason.GOAL_ACHIEVED)
        reply = await self._llm.complete(
            LLMRequest(
                model="fake/sim",
                messages=[Message(role="user", content=f"turn {len(dialogue)}")],
                seed=self._brief.seed,
            )
        )
        return SimTurn(text=reply.text)


def fake_simulator_factory(brief: TrialBrief, llm: LLMProvider) -> CustomerSimulator:
    return FakeSimulator(brief, llm)


@dataclass
class FakeDriver:
    """Alternates simulator turns with a canned agent reply; calls the world tool once."""

    channel: Channel = Channel.CHAT
    sandbox: FakeSandbox | None = None
    outcome: str = "ok"
    fail_runs: int = 0
    hang: bool = False
    runs: int = 0

    async def run(
        self,
        ctx: SessionContext,
        brief: TrialBrief,
        simulator: CustomerSimulator,
        sink: EventSink,
    ) -> DriverResult:
        self.runs += 1
        if self.runs <= self.fail_runs:
            raise RuntimeError("driver lost the connection")
        if self.hang:
            await anyio.sleep_forever()
        dialogue: list[DialogueTurn] = []
        while True:
            step = await simulator.next_turn(dialogue)
            if isinstance(step, SimEnd):
                break
            sink.emit("user", UtterancePayload(text=step.text, turn=len(dialogue)))
            dialogue.append(DialogueTurn(speaker="user", text=step.text))
            sink.emit("agent", UtterancePayload(text="sure", turn=len(dialogue), latency_ms=5))
            dialogue.append(DialogueTurn(speaker="agent", text="sure"))
        if self.sandbox is not None and self.sandbox.sessions:
            self.sandbox.sessions[-1].call_tool("reschedule", "C-1042", "2026-10-28T09:00")
        if self.outcome == "agent_error":
            return DriverResult(
                status="agent_error", end_reason=EndReason.ERROR, turns=len(dialogue), error="500"
            )
        return DriverResult(status="ok", end_reason=EndReason.GOAL_ACHIEVED, turns=len(dialogue))


@dataclass
class FakeGrader:
    """Passes when the trial status is ok; optionally asks a judge model first."""

    name: str = "fake"
    version: str = "1"
    judge: bool = False
    raises: bool = False
    contexts: list[GradeContext] = field(default_factory=list)

    async def grade(self, ctx: GradeContext) -> Grade:
        self.contexts.append(ctx)
        if self.raises:
            raise ValueError("grader bug")
        cost = Decimal(0)
        if self.judge:
            assert ctx.llm is not None
            reply = await ctx.llm.complete(
                LLMRequest(
                    model=ctx.judge_model or "fake/judge",
                    messages=[Message(role="user", content="judge this")],
                )
            )
            cost = reply.cost_usd
        passed = ctx.trial.status == "ok"
        return Grade(
            id=GradeId(new_id()),
            trial_id=ctx.trial.id,
            grader=self.name,
            grader_version=self.version,
            criterion_index=ctx.criterion_index,
            value=passed,
            passed=passed,
            cost_usd=cost,
        )


class PricedLLM:
    """Every reply costs ``cost_usd``, so tests can drive the budget."""

    def __init__(self, cost_usd: Decimal) -> None:
        self.cost_usd = cost_usd
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            model=request.model,
            text=f"reply {self.calls}",
            usage=LLMUsage(input_tokens=10, output_tokens=5),
            cost_usd=self.cost_usd,
        )


def graders_for(*graders: FakeGrader) -> TrialGrader:
    """Grades like ``hecklr_grade.grade_trial`` minus its error handling: grader i answers
    criterion i, extra graders are diagnostics."""

    async def grade(ctx: GradeContext) -> list[Grade]:
        grades = []
        for index, grader in enumerate(graders):
            criterion_index = index if index < len(ctx.scenario.success) else None
            bound = dataclasses.replace(
                ctx,
                criterion=ctx.scenario.success[index] if criterion_index is not None else None,
                criterion_index=criterion_index,
            )
            grades.append(await grader.grade(bound))
        return grades

    return grade


def make_collaborators(
    *,
    sandbox: FakeSandbox | None = None,
    driver: FakeDriver | None = None,
    llm: LLMProvider | None = None,
    graders: Sequence[FakeGrader] = (),
) -> Collaborators:
    sandbox = sandbox or FakeSandbox()
    driver = driver or FakeDriver(sandbox=sandbox)
    model = llm or PricedLLM(Decimal(0))
    return Collaborators(
        sandbox=sandbox,
        drivers={Channel.CHAT: driver, Channel.EMAIL: FakeDriver(channel=Channel.EMAIL)},
        simulator_factory=fake_simulator_factory,
        simulator_llm=model,
        judge_llm=model,
        grade_trial=graders_for(*graders),
    )


# --- helpers ------------------------------------------------------------------------------------


def _planned(suite: Suite, *, usd: Decimal | None = None, concurrency: int = 4) -> Run:
    run = plan_run(suite).run
    return run.model_copy(update={"budget": Budget(usd=usd, concurrency=concurrency)})


async def _execute(
    tmp_path: Path,
    suite: Suite,
    collaborators: Collaborators,
    run: Run | None = None,
    *,
    deadline_grace_s: float = 30.0,
    tracer: Tracer | None = None,
) -> tuple[Run, ResultStore]:
    store = ResultStore(tmp_path / "runs")
    executor = RunExecutor(
        suite,
        collaborators,
        store,
        retry=NO_WAIT,
        deadline_grace_s=deadline_grace_s,
        tracer=tracer,
    )
    final = await executor.execute(run or _planned(suite))
    return final, store


def _with_limits(suite: Suite, limits: ScenarioLimits) -> Suite:
    cells = tuple(
        dataclasses.replace(cell, scenario=cell.scenario.model_copy(update={"limits": limits}))
        for cell in suite.cells
    )
    return dataclasses.replace(suite, cells=cells)


# --- the trial loop -----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ws1_run_executes_grades_and_stores_every_trial(
    tmp_path: Path, ci_suite: Suite
) -> None:
    sandbox = FakeSandbox()
    grader, judge = FakeGrader(name="tool_called"), FakeGrader(name="judge:resolved", judge=True)
    collaborators = make_collaborators(sandbox=sandbox, graders=[grader, judge])
    run = _planned(ci_suite)

    final, store = await _execute(tmp_path, ci_suite, collaborators, run)
    results = await store.load_run(run.id)
    store.close()

    assert final.status is RunStatus.COMPLETED
    assert final.counts.model_dump() == {
        "planned": 4,
        "ok": 4,
        "agent_error": 0,
        "harness_error": 0,
        "timeout": 0,
    }
    assert results.run == final
    assert {t.spec for t in results.trials} == set(run.matrix)
    for trial in results.trials:
        assert trial.status == "ok"
        assert trial.end_reason is EndReason.GOAL_ACHIEVED
        assert (trial.turns, trial.contradictions, trial.attempts) == (4, 1, 1)
        assert trial.world_before.collections["appointments"]["C-1042"]["slot"] == (
            "2026-10-23T09:00"
        )
        assert trial.world_after.collections["appointments"]["C-1042"]["slot"] == (
            "2026-10-28T09:00"
        )
        events = results.events_of(trial)
        assert [e.kind for e in events] == ["utterance"] * 4 + ["tool_call", "tool_result"]
        assert [e.seq for e in events] == list(range(6))
        grades = results.grades_of(trial)
        assert [(g.grader, g.criterion_index, g.passed) for g in grades] == [
            ("tool_called", 0, True),
            ("judge:resolved", 1, True),
        ]
    assert all(session.closed for session in sandbox.sessions)
    # Graders got the criterion they answer, the trial's events and a judge model handle.
    ctx = judge.contexts[0]
    assert ctx.criterion is not None
    assert ctx.criterion.type == "judge"
    assert ctx.judge_model == "fake/judge"
    assert ctx.llm is not None
    assert len(ctx.events) == 6
    # Artifacts.
    run_dir = store.run_dir(run.id)
    assert Run.model_validate_json((run_dir / RUN_JSON).read_text()) == final
    assert {p.name for p in run_dir.glob("*.parquet")} == {
        "trials.parquet",
        "events.parquet",
        "grades.parquet",
    }


@pytest.mark.anyio
async def test_session_context_carries_trial_seed_and_world_urls(
    tmp_path: Path, ci_suite: Suite
) -> None:
    seen: list[tuple[SessionContext, TrialBrief]] = []

    @dataclass
    class Recording(FakeDriver):
        async def run(
            self,
            ctx: SessionContext,
            brief: TrialBrief,
            simulator: CustomerSimulator,
            sink: EventSink,
        ) -> DriverResult:
            seen.append((ctx, brief))
            return await super().run(ctx, brief, simulator, sink)

    final, store = await _execute(tmp_path, ci_suite, make_collaborators(driver=Recording()))
    results = await store.load_run(final.id)
    store.close()
    by_id = {trial.id: trial for trial in results.trials}
    for ctx, brief in seen:
        assert ctx.trial_id == brief.trial_id
        assert ctx.seed == brief.seed == by_id[brief.trial_id].seed
        assert ctx.world_mcp_url == "http://world.test/mcp"
        assert ctx.world_http_url == "http://world.test/api"
        assert ctx.headers() == {"X-Hecklr-Trial-Id": brief.trial_id}


@pytest.mark.anyio
async def test_agent_errors_are_recorded_graded_and_not_retried(
    tmp_path: Path, ci_suite: Suite
) -> None:
    driver = FakeDriver(outcome="agent_error")
    final, store = await _execute(
        tmp_path, ci_suite, make_collaborators(driver=driver, graders=[FakeGrader()])
    )
    results = await store.load_run(final.id)
    store.close()
    assert driver.runs == 4
    assert final.counts.agent_error == 4
    assert {(t.status, t.error, t.attempts) for t in results.trials} == {("agent_error", "500", 1)}
    assert len(results.grades) == 4
    assert not any(g.passed for g in results.grades)


# --- harness errors and retries -----------------------------------------------------------------


@pytest.mark.anyio
async def test_harness_errors_are_retried_then_succeed(tmp_path: Path, ci_suite: Suite) -> None:
    sandbox = FakeSandbox(fail_opens=2)
    run = _planned(ci_suite, concurrency=1)
    final, store = await _execute(tmp_path, ci_suite, make_collaborators(sandbox=sandbox), run)
    results = await store.load_run(final.id)
    store.close()
    assert final.status is RunStatus.COMPLETED
    assert final.counts.ok == 4
    first = min(results.trials, key=lambda t: t.started_at)
    assert sorted(t.attempts for t in results.trials) == [1, 1, 1, 3]
    assert first.attempts == 3
    # Only the successful attempt's events are kept.
    assert "error" not in {e.kind for e in results.events_of(first)}


@pytest.mark.anyio
async def test_harness_error_after_three_attempts_is_recorded_and_excluded(
    tmp_path: Path, ci_suite: Suite
) -> None:
    sandbox = FakeSandbox(fail_opens=100)
    final, store = await _execute(
        tmp_path, ci_suite, make_collaborators(sandbox=sandbox, graders=[FakeGrader()])
    )
    results = await store.load_run(final.id)
    store.close()
    assert sandbox.opens == 12
    assert final.status is RunStatus.COMPLETED
    assert final.counts.harness_error == 4
    assert final.counts.ok == 0
    for trial in results.trials:
        assert trial.status == "harness_error"
        assert trial.attempts == 3
        assert trial.end_reason is EndReason.ERROR
        assert trial.error == "ConnectionError: world server down"
        assert trial.world_before.collections == {}
        (event,) = results.events_of(trial)
        assert isinstance(event.payload, ErrorPayload)
        assert event.payload.source == "harness"
        assert event.payload.retryable
    assert results.grades == []


@pytest.mark.anyio
async def test_driver_failure_keeps_world_snapshots_and_closes_the_world(
    tmp_path: Path, ci_suite: Suite
) -> None:
    sandbox = FakeSandbox()
    driver = FakeDriver(sandbox=sandbox, fail_runs=100)
    final, store = await _execute(
        tmp_path, ci_suite, make_collaborators(sandbox=sandbox, driver=driver)
    )
    results = await store.load_run(final.id)
    store.close()
    assert len(sandbox.sessions) == 12
    assert all(session.closed for session in sandbox.sessions)
    trial = results.trials[0]
    assert trial.world_before.collections["appointments"]
    assert trial.world_after == trial.world_before.model_copy(
        update={"taken_at_ms": trial.world_after.taken_at_ms}
    )
    assert trial.error == "RuntimeError: driver lost the connection"


@pytest.mark.anyio
async def test_configuration_errors_are_not_retried(tmp_path: Path, ci_suite: Suite) -> None:
    sandbox = FakeSandbox(fail_opens=100, error=lambda: UnknownPriceError("no price"))
    final, _ = await _execute(tmp_path, ci_suite, make_collaborators(sandbox=sandbox))
    assert sandbox.opens == 4
    assert final.counts.harness_error == 4


def test_retryable_classification() -> None:
    assert is_retryable(ConnectionError())
    assert is_retryable(LLMProviderError("429", retryable=True, status_code=429))
    assert not is_retryable(LLMProviderError("400", retryable=False, status_code=400))
    assert not is_retryable(UnknownPriceError("x"))


def test_retry_backoff_is_exponential_seeded_and_capped() -> None:
    policy = RetryPolicy(base_delay_s=1.0, max_delay_s=3.0)
    first, second = policy.delay_s(42, 1), policy.delay_s(42, 2)
    assert 0.5 <= first < 1.5
    assert 1.0 <= second <= 3.0
    assert policy.delay_s(42, 1) == first
    assert policy.delay_s(43, 1) != first
    assert policy.delay_s(42, 10) == 3.0
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="delays"):
        RetryPolicy(base_delay_s=2.0, max_delay_s=1.0)


@pytest.mark.anyio
async def test_hung_trial_hits_the_hard_deadline(tmp_path: Path, ci_suite: Suite) -> None:
    suite = _with_limits(ci_suite, ScenarioLimits(max_turns=4, max_duration_s=1))
    sandbox = FakeSandbox()
    driver = FakeDriver(hang=True)
    started = time.monotonic()
    final, store = await _execute(
        tmp_path,
        suite,
        make_collaborators(sandbox=sandbox, driver=driver, graders=[FakeGrader()]),
        deadline_grace_s=0.0,
    )
    results = await store.load_run(final.id)
    store.close()
    assert time.monotonic() - started < 5
    assert driver.runs == 4
    assert final.counts.timeout == 4
    for trial in results.trials:
        assert (trial.status, trial.end_reason, trial.attempts) == (
            "timeout",
            EndReason.MAX_DURATION,
            1,
        )
        assert trial.duration_ms >= 1000
    assert all(session.closed for session in sandbox.sessions)
    assert len(results.grades) == 4


# --- grading ------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_trial_grader_gets_an_unbound_metered_context_and_its_grades_are_stored(
    tmp_path: Path, ci_suite: Suite
) -> None:
    contexts: list[GradeContext] = []
    inner = graders_for(FakeGrader(name="first"), FakeGrader(name="second"))

    async def grade(ctx: GradeContext) -> list[Grade]:
        contexts.append(ctx)
        return list(await inner(ctx))

    collaborators = dataclasses.replace(make_collaborators(), grade_trial=grade)
    final, store = await _execute(tmp_path, ci_suite, collaborators)
    results = await store.load_run(final.id)
    store.close()
    assert len(contexts) == len(results.trials) == 4
    assert {(ctx.criterion, ctx.criterion_index) for ctx in contexts} == {(None, None)}
    assert all(ctx.llm is not None and ctx.judge_model == "fake/judge" for ctx in contexts)
    assert [g.grader for g in results.grades] == ["first", "second"] * 4


@pytest.mark.anyio
async def test_failing_trial_grader_fails_the_run_and_records_it(
    tmp_path: Path, ci_suite: Suite
) -> None:
    async def broken(ctx: GradeContext) -> list[Grade]:
        raise KeyError("unknown rubric")

    collaborators = dataclasses.replace(make_collaborators(), grade_trial=broken)
    store = ResultStore(tmp_path / "runs")
    run = _planned(ci_suite)
    with pytest.raises(ExceptionGroup) as caught:
        await RunExecutor(ci_suite, collaborators, store, retry=NO_WAIT).execute(run)
    # Every trial that reached grading before the group was cancelled reports the same bug.
    _, others = caught.value.split(KeyError)
    assert others is None
    assert store.list_runs()[0].status is RunStatus.FAILED
    store.close()


# --- cost and budget ----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_trial_cost_includes_simulator_and_judge_spend(
    tmp_path: Path, ci_suite: Suite
) -> None:
    llm = PricedLLM(Decimal("0.01"))
    collaborators = make_collaborators(llm=llm, graders=[FakeGrader(judge=True)])
    final, store = await _execute(tmp_path, ci_suite, collaborators)
    results = await store.load_run(final.id)
    store.close()
    # 2 simulator turns + 1 judge call per trial.
    assert {t.cost_usd for t in results.trials} == {Decimal("0.03")}
    assert {g.cost_usd for g in results.grades} == {Decimal("0.01")}
    assert final.spent_usd == Decimal("0.12")


@pytest.mark.anyio
async def test_budget_exceeded_mid_run_stops_cleanly_and_keeps_finished_trials(
    tmp_path: Path, ci_suite: Suite
) -> None:
    llm = PricedLLM(Decimal(1))
    run = _planned(ci_suite, usd=Decimal("2.5"), concurrency=1)
    sandbox = FakeSandbox()
    final, store = await _execute(
        tmp_path, ci_suite, make_collaborators(sandbox=sandbox, llm=llm), run
    )
    results = await store.load_run(final.id)
    store.close()
    assert final.status is RunStatus.BUDGET_EXCEEDED
    # Trial 1 spends $2; trial 2's first call takes spend to $3 and the run stops.
    assert llm.calls == 3
    assert final.spent_usd == Decimal(3)
    assert final.counts.ok == 1
    assert final.counts.finished == 1
    assert final.counts.planned == 4
    assert [t.cost_usd for t in results.trials] == [Decimal(2)]
    assert all(session.closed for session in sandbox.sessions)


@dataclass
class SwallowingGrader(FakeGrader):
    """A judge that turns any model failure into an error grade instead of raising."""

    async def grade(self, ctx: GradeContext) -> Grade:
        try:
            return await super().grade(ctx)
        except Exception as exc:  # noqa: BLE001 -- models a grader that swallows every failure
            return Grade(
                id=GradeId(new_id()),
                trial_id=ctx.trial.id,
                grader=self.name,
                grader_version=self.version,
                status="error",
                value=False,
                passed=False,
                rationale=str(exc),
            )


@pytest.mark.anyio
@pytest.mark.parametrize("grader", [FakeGrader(judge=True), SwallowingGrader(judge=True)])
async def test_budget_exceeded_while_grading_drops_the_trial(
    tmp_path: Path, ci_suite: Suite, grader: FakeGrader
) -> None:
    llm = PricedLLM(Decimal(1))
    run = _planned(ci_suite, usd=Decimal("2.5"), concurrency=1)
    # The judge call is the third call and crosses the budget. Whether the grader raises or
    # swallows the error, the half-graded trial is dropped rather than stored.
    collaborators = make_collaborators(llm=llm, graders=[grader])
    final, store = await _execute(tmp_path, ci_suite, collaborators, run)
    results = await store.load_run(final.id)
    store.close()
    assert final.status is RunStatus.BUDGET_EXCEEDED
    assert results.trials == []
    assert results.grades == []
    assert final.spent_usd == Decimal(3)


@pytest.mark.anyio
async def test_concurrency_cap_is_respected(tmp_path: Path, project_dir: Path) -> None:
    suite = resolve_suite(load_project(project_dir / "hecklr.yaml"), "ci", trials=10)
    active = 0
    peak = 0

    @dataclass
    class Slow(FakeDriver):
        async def run(
            self,
            ctx: SessionContext,
            brief: TrialBrief,
            simulator: CustomerSimulator,
            sink: EventSink,
        ) -> DriverResult:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await anyio.sleep(0.01)
            active -= 1
            return await super().run(ctx, brief, simulator, sink)

    run = _planned(suite, concurrency=3)
    final, store = await _execute(tmp_path, suite, make_collaborators(driver=Slow()), run)
    store.close()
    assert final.counts.ok == 20
    assert peak == 3


@pytest.mark.anyio
async def test_budget_error_wrapped_by_a_driver_still_stops_the_run(
    tmp_path: Path, ci_suite: Suite
) -> None:
    @dataclass
    class Wrapping(FakeDriver):
        async def run(
            self,
            ctx: SessionContext,
            brief: TrialBrief,
            simulator: CustomerSimulator,
            sink: EventSink,
        ) -> DriverResult:
            try:
                return await super().run(ctx, brief, simulator, sink)
            except Exception as exc:
                raise RuntimeError("simulator failed") from exc

    llm = PricedLLM(Decimal(1))
    run = _planned(ci_suite, usd=Decimal("0.5"), concurrency=1)
    final, store = await _execute(
        tmp_path, ci_suite, make_collaborators(driver=Wrapping(), llm=llm), run
    )
    store.close()
    assert final.status is RunStatus.BUDGET_EXCEEDED
    assert final.counts.finished == 0
    assert llm.calls == 1


@pytest.mark.anyio
async def test_exhausted_budget_starts_no_adaptive_trials(tmp_path: Path, ci_suite: Suite) -> None:
    store = ResultStore(tmp_path / "runs")
    sandbox = FakeSandbox()
    executor = RunExecutor(ci_suite, make_collaborators(sandbox=sandbox), store, retry=NO_WAIT)
    spent = _planned(ci_suite, usd=Decimal(1)).model_copy(
        update={"spent_usd": Decimal(2), "status": RunStatus.BUDGET_EXCEEDED}
    )
    extra = extend_matrix(spent.matrix, spent.seed, ["reschedule"], extra_trials=1)
    final = await executor.run_additional(spent, extra)
    store.close()
    assert final.status is RunStatus.BUDGET_EXCEEDED
    assert sandbox.opens == 0


@pytest.mark.anyio
async def test_failed_snapshot_after_a_failure_keeps_the_original_error(
    tmp_path: Path, ci_suite: Suite
) -> None:
    class BrittleSession(FakeWorldSession):
        snapshots = 0

        def snapshot(self) -> WorldSnapshot:
            self.snapshots += 1
            if self.snapshots > 1:
                raise OSError("store gone")
            return super().snapshot()

    @dataclass
    class BrittleSandbox(FakeSandbox):
        async def open(self, brief: TrialBrief, world: World, sink: EventSink) -> FakeWorldSession:
            session = BrittleSession(world, sink)
            self.sessions.append(session)
            return session

    sandbox = BrittleSandbox()
    driver = FakeDriver(fail_runs=100)
    final, store = await _execute(
        tmp_path, ci_suite, make_collaborators(sandbox=sandbox, driver=driver)
    )
    results = await store.load_run(final.id)
    store.close()
    trial = results.trials[0]
    assert trial.error == "RuntimeError: driver lost the connection"
    assert trial.world_after == trial.world_before


# --- run lifecycle ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cancelled_run_is_recorded(tmp_path: Path, ci_suite: Suite) -> None:
    store = ResultStore(tmp_path / "runs")
    executor = RunExecutor(
        ci_suite, make_collaborators(driver=FakeDriver(hang=True)), store, retry=NO_WAIT
    )
    with capture_logs() as logs, anyio.move_on_after(0.2):
        await executor.execute(_planned(ci_suite))
    (run,) = store.list_runs()
    store.close()
    assert run.status is RunStatus.CANCELLED
    # Issue #35: Ctrl-C is not an error; the CLI prints its own one-line notice.
    run_end = next(entry for entry in logs if entry["event"] == "run.end")
    assert (run_end["log_level"], run_end["status"]) == ("info", "cancelled")


@dataclass
class HangsAfter(FakeDriver):
    """Finishes its first ``finished`` trials, then hangs: a run stopped partway."""

    finished: int = 2

    async def run(
        self,
        ctx: SessionContext,
        brief: TrialBrief,
        simulator: CustomerSimulator,
        sink: EventSink,
    ) -> DriverResult:
        self.hang = self.runs >= self.finished
        return await super().run(ctx, brief, simulator, sink)


@pytest.mark.anyio
async def test_issue117_a_cancelled_run_keeps_its_counts_exports_and_closes_its_store(
    tmp_path: Path, ci_suite: Suite
) -> None:
    # SIGTERM in `hecklr run` cancels the run like Ctrl-C: whatever finished must be kept.
    runs_dir = tmp_path / "runs"
    planned = _planned(ci_suite)
    with anyio.CancelScope() as scope:
        async with CountingStore(runs_dir, wanted=2) as store:
            executor = RunExecutor(
                ci_suite, make_collaborators(driver=HangsAfter(finished=2)), store, retry=NO_WAIT
            )
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(executor.execute, planned)
                await store.reached.wait()
                scope.cancel()
    run_dir = runs_dir / planned.id
    assert sorted(path.name for path in run_dir.glob("*.parquet")) == [
        "events.parquet",
        "grades.parquet",
        "trials.parquet",
    ]
    # The store was closed although the scope around it was cancelled: no WAL is left.
    assert not list(run_dir.glob("*.wal"))
    async with ResultStore(runs_dir) as reopened:
        (run,) = reopened.list_runs()
        results = await reopened.load_run(run.id)
    assert run.status is RunStatus.CANCELLED
    assert run.counts.ok == len(results.trials) == 2


class CountingStore(ResultStore):
    """Sets ``reached`` once ``wanted`` trials are stored."""

    def __init__(self, runs_dir: Path, wanted: int) -> None:
        super().__init__(runs_dir)
        self.reached = anyio.Event()
        self._left = wanted

    async def append_trial_result(
        self, trial: Trial, events: Sequence[Event], grades: Sequence[Grade]
    ) -> None:
        await super().append_trial_result(trial, events, grades)
        self._left -= 1
        if self._left == 0:
            self.reached.set()


@pytest.mark.anyio
async def test_missing_channel_driver_is_refused(tmp_path: Path, project_dir: Path) -> None:
    suite = resolve_suite(load_project(project_dir / "hecklr.yaml"), "all")
    collaborators = dataclasses.replace(make_collaborators(), drivers={Channel.CHAT: FakeDriver()})
    with pytest.raises(ValueError, match=r"no channel driver for \['email'\]"):
        await _execute(tmp_path, suite, collaborators)


@pytest.mark.anyio
async def test_adaptive_trials_append_to_the_same_run(tmp_path: Path, ci_suite: Suite) -> None:
    store = ResultStore(tmp_path / "runs")
    executor = RunExecutor(ci_suite, make_collaborators(), store, retry=NO_WAIT)
    first = await executor.execute(_planned(ci_suite))
    extra = extend_matrix(first.matrix, first.seed, ["reschedule"], extra_trials=1)
    second = await executor.run_additional(first, extra)
    results = await store.load_run(first.id)

    assert second.id == first.id
    assert second.adaptive
    assert second.matrix == [*first.matrix, *extra]
    assert second.counts.planned == 6
    assert second.counts.ok == 6
    assert results.run == second
    assert sorted(t.index for t in results.trials) == [0, 0, 1, 1, 2, 2]
    with pytest.raises(ValueError, match="already in the run's matrix"):
        await executor.run_additional(second, extra)
    store.close()


@pytest.mark.anyio
async def test_same_seed_replays_the_same_trial_seeds(tmp_path: Path, ci_suite: Suite) -> None:
    first, store_a = await _execute(tmp_path / "a", ci_suite, make_collaborators())
    second, store_b = await _execute(tmp_path / "b", ci_suite, make_collaborators())
    a = await store_a.load_run(first.id)
    b = await store_b.load_run(second.id)
    store_a.close()
    store_b.close()
    assert sorted((t.spec.key, t.seed) for t in a.trials) == sorted(
        (t.spec.key, t.seed) for t in b.trials
    )


# --- observability ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_logs_carry_run_and_trial_ids(tmp_path: Path, ci_suite: Suite) -> None:
    with capture_logs(processors=[merge_contextvars]) as logs:
        final, store = await _execute(tmp_path, ci_suite, make_collaborators())
    store.close()
    trial_logs = [entry for entry in logs if entry["event"] == "trial.end"]
    assert len(trial_logs) == 4
    assert all(entry["run_id"] == final.id for entry in trial_logs)
    assert len({entry["trial_id"] for entry in trial_logs}) == 4
    run_end = next(entry for entry in logs if entry["event"] == "run.end")
    assert (run_end["run_id"], run_end["status"]) == (final.id, "completed")
    assert "trial_id" not in run_end
    assert structlog.contextvars.get_contextvars() == {}


@pytest.mark.anyio
async def test_run_and_trial_spans(tmp_path: Path, ci_suite: Suite) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    final, store = await _execute(tmp_path, ci_suite, make_collaborators(), tracer=tracer)
    store.close()
    spans = exporter.get_finished_spans()
    (run_span,) = [span for span in spans if span.name == "hecklr.run"]
    trial_spans = [span for span in spans if span.name == "hecklr.trial"]
    assert len(trial_spans) == 4
    assert run_span.attributes is not None
    assert run_span.attributes["hecklr.run_id"] == final.id
    assert run_span.attributes["hecklr.status"] == "completed"
    for span in trial_spans:
        assert span.parent is not None
        assert span.parent.span_id == run_span.context.span_id
        assert span.attributes is not None
        assert span.attributes["hecklr.status"] == "ok"


# --- performance (WS1 Done criterion) -----------------------------------------------------------


@pytest.mark.perf
@pytest.mark.anyio
async def test_ws1_thousand_fake_trials_under_sixty_seconds(
    tmp_path: Path, project_dir: Path
) -> None:
    suite = resolve_suite(load_project(project_dir / "hecklr.yaml"), "ci", trials=500)
    run = _planned(suite, concurrency=32)
    assert len(run.matrix) == 1000
    collaborators = make_collaborators(graders=[FakeGrader(), FakeGrader(judge=True)])
    started = time.monotonic()
    final, store = await _execute(tmp_path, suite, collaborators, run)
    elapsed = time.monotonic() - started
    results = await store.load_run(final.id)
    store.close()
    assert final.counts.ok == 1000
    assert len(results.trials) == 1000
    assert len(results.grades) == 2000
    assert elapsed < 60, f"1,000 fake trials took {elapsed:.1f} s"
