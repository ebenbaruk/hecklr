"""Run lifecycle steps 3 and 4, Execute and Grade: run a matrix of trials and record the results.

Each trial follows the loop in ``hecklr_schemas.trial`` (ADR 0006): open the world, snapshot it,
let the channel driver run the simulator against the agent, snapshot again, close the world;
then grade. The executor only knows the Protocols; concrete simulators, worlds, drivers and
graders are injected by the caller (the ``hecklr`` CLI wires them from ``hecklr.yaml``).
"""

import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

import anyio
import structlog
from anyio.abc import TaskGroup
from opentelemetry import trace
from structlog.contextvars import bound_contextvars

from hecklr_core.errors import BudgetExceededError
from hecklr_core.loading import Suite
from hecklr_core.metering import MeteredProvider, RunBudget, TrialSpend
from hecklr_core.sink import TrialEventSink
from hecklr_core.storage import ResultStore
from hecklr_llm import LLMError, LLMProviderError
from hecklr_schemas import (
    Channel,
    ChannelDriver,
    Clock,
    CustomerSimulator,
    DriverResult,
    EndReason,
    ErrorPayload,
    Event,
    Grade,
    GradeContext,
    LLMProvider,
    MonotonicClock,
    Persona,
    Run,
    RunCounts,
    RunStatus,
    Scenario,
    SessionContext,
    SimulatorFactory,
    Trial,
    TrialBrief,
    TrialId,
    TrialSpec,
    TrialStatus,
    WorldSandbox,
    WorldSession,
    WorldSnapshot,
    new_id,
)

WORLD_CLOSE_TIMEOUT_S: Final = 10.0
"""How long closing a world may take, even while the trial is being cancelled."""

_log = structlog.get_logger(__name__)


TrialGrader = Callable[[GradeContext], Awaitable[Sequence[Grade]]]
"""Grades one finished trial and returns every grade, in a stable order.

The executor passes a context without a criterion (``criterion`` and ``criterion_index`` are
``None``), with ``llm`` metered against the trial's budget. The callable owns grading entirely:
which graders run, their order and concurrency, and turning a failing grader into an ``error``
grade (``hecklr_grade.grade_trial`` does all of this). If the callable itself raises, the run
fails: that is a harness bug, not something one trial can absorb.
"""


@dataclass(frozen=True, slots=True)
class Collaborators:
    """The components a run drives, all behind ``hecklr-schemas`` Protocols."""

    sandbox: WorldSandbox
    drivers: Mapping[Channel, ChannelDriver]
    simulator_factory: SimulatorFactory
    simulator_llm: LLMProvider
    judge_llm: LLMProvider
    grade_trial: TrialGrader


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Harness errors are retried with exponential backoff and seeded jitter."""

    max_attempts: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 8.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {self.max_attempts}")
        if self.base_delay_s < 0 or self.max_delay_s < self.base_delay_s:
            raise ValueError("delays must satisfy 0 <= base_delay_s <= max_delay_s")

    def delay_s(self, trial_seed: int, attempt: int) -> float:
        """Wait before retrying after failed ``attempt`` (1-based): base x 2^(attempt-1) x jitter.

        Jitter in [0.5, 1.5) is drawn from the trial seed, so retries spread out but replay
        identically.
        """
        jitter = random.Random(f"{trial_seed}:retry:{attempt}").uniform(0.5, 1.5)  # noqa: S311 -- backoff jitter, not cryptography
        delay: float = self.base_delay_s * 2 ** (attempt - 1) * jitter
        return min(self.max_delay_s, delay)


def is_retryable(exc: BaseException) -> bool:
    """Whether a harness failure is worth another attempt.

    Model-provider errors say so themselves; other provider-layer errors (unknown price, missing
    key) are configuration problems that a retry cannot fix. Everything else is assumed
    transient.
    """
    if isinstance(exc, LLMProviderError):
        return exc.retryable
    return not isinstance(exc, LLMError)


@dataclass
class _Attempt:
    """What one attempt of a trial produced."""

    sink: TrialEventSink
    started_at: datetime
    before: WorldSnapshot | None = None
    after: WorldSnapshot | None = None
    simulator: CustomerSimulator | None = None
    result: DriverResult | None = None
    failure: Exception | None = None
    timed_out: bool = False
    duration_ms: int = 0

    @property
    def status(self) -> TrialStatus:
        if self.timed_out:
            return "timeout"
        if self.failure is not None or self.result is None:
            return "harness_error"
        return self.result.status


@dataclass
class _Tally:
    counts: RunCounts
    outcomes: dict[TrialStatus, int] = field(default_factory=dict)

    def add(self, status: TrialStatus) -> None:
        self.outcomes[status] = self.outcomes.get(status, 0) + 1

    def snapshot(self, planned: int) -> RunCounts:
        base = self.counts
        return RunCounts(
            planned=planned,
            ok=base.ok + self.outcomes.get("ok", 0),
            agent_error=base.agent_error + self.outcomes.get("agent_error", 0),
            harness_error=base.harness_error + self.outcomes.get("harness_error", 0),
            timeout=base.timeout + self.outcomes.get("timeout", 0),
        )


class RunExecutor:
    """Runs trials concurrently and records them in a ``ResultStore``.

    Concurrency is capped by ``run.budget.concurrency``; model spend is capped by
    ``run.budget.usd``: once crossed, no new trial starts, in-flight trials are cancelled, and the
    run ends ``budget_exceeded`` with every finished trial kept.
    """

    def __init__(
        self,
        suite: Suite,
        collaborators: Collaborators,
        store: ResultStore,
        *,
        retry: RetryPolicy | None = None,
        deadline_grace_s: float = 30.0,
        clock_factory: Callable[[], Clock] = MonotonicClock,
        tracer: trace.Tracer | None = None,
    ) -> None:
        """Create an executor.

        Args:
            suite: Scenarios, personas and world of the run.
            collaborators: Components the trials drive.
            store: Where results are written.
            retry: Harness-error retry policy.
            deadline_grace_s: Added to ``limits.max_duration_s`` to get the hard deadline after
                which the executor stops a trial the driver failed to end (``timeout``).
            clock_factory: Builds each trial's harness clock.
            tracer: OpenTelemetry tracer; the global one (a no-op unless configured) by default.
        """
        self._suite = suite
        self._collaborators = collaborators
        self._store = store
        self._retry = retry or RetryPolicy()
        self._deadline_grace_s = deadline_grace_s
        self._clock_factory = clock_factory
        self._tracer = tracer or trace.get_tracer("hecklr_core")

    async def execute(self, run: Run) -> Run:
        """Run every trial of ``run.matrix`` and return the final run record."""
        return await self._run(run, run.matrix)

    async def run_additional(self, run: Run, specs: Sequence[TrialSpec]) -> Run:
        """Adaptive hook: run extra trials and append them to the same run.

        ``specs`` come from ``planning.extend_matrix``; the run is re-recorded with the extended
        matrix and ``adaptive=True``. The budget carries over from ``run.spent_usd``.

        Raises:
            ValueError: a spec repeats a cell already in the matrix.
        """
        existing = {spec.key for spec in run.matrix}
        clashes = sorted(str(spec.key) for spec in specs if spec.key in existing)
        if clashes:
            raise ValueError(f"cells already in the run's matrix: {clashes}")
        extended = run.model_copy(update={"matrix": [*run.matrix, *specs], "adaptive": True})
        return await self._run(extended, specs)

    # --- run level ----------------------------------------------------------------------------

    async def _run(self, run: Run, specs: Sequence[TrialSpec]) -> Run:
        self._check_drivers(specs)
        budget = RunBudget(run.budget.usd, run.spent_usd)
        tally = _Tally(run.counts)
        planned = len(run.matrix)
        running = run.model_copy(
            update={"status": RunStatus.RUNNING, "counts": tally.snapshot(planned)}
        )
        attributes: dict[str, str | int] = {"hecklr.run_id": run.id, "hecklr.trials": len(specs)}
        with (
            bound_contextvars(run_id=run.id),
            self._tracer.start_as_current_span("hecklr.run", attributes=attributes) as span,
        ):
            await self._store.write_run(running)
            _log.info("run.start", suite=run.suite, trials=len(specs), budget_usd=run.budget.usd)
            try:
                await self._run_specs(run, specs, budget, tally)
            except BaseException as exc:
                status = (
                    RunStatus.CANCELLED
                    if isinstance(exc, anyio.get_cancelled_exc_class())
                    else RunStatus.FAILED
                )
                with anyio.CancelScope(shield=True):
                    await self._store.write_run(self._final(running, status, budget, tally))
                    if status is RunStatus.CANCELLED:
                        # Ctrl-C or SIGTERM: the trials that finished stay readable (#117).
                        await self._store.export(run.id)
                # A cancellation (Ctrl-C, a caller's deadline) is the user's decision, not an error.
                log = _log.info if status is RunStatus.CANCELLED else _log.error
                log("run.end", status=status.value, error=repr(exc))
                raise
            status = RunStatus.BUDGET_EXCEEDED if budget.exceeded else RunStatus.COMPLETED
            final = self._final(running, status, budget, tally)
            await self._store.write_run(final)
            await self._store.export(run.id)
            span.set_attribute("hecklr.status", status.value)
            _log.info(
                "run.end",
                status=status.value,
                spent_usd=str(final.spent_usd),
                **final.counts.model_dump(),
            )
            return final

    def _check_drivers(self, specs: Sequence[TrialSpec]) -> None:
        missing = sorted(
            {spec.channel.value for spec in specs} - {c.value for c in self._collaborators.drivers}
        )
        if missing:
            raise ValueError(f"no channel driver for {missing}")

    @staticmethod
    def _final(running: Run, status: RunStatus, budget: RunBudget, tally: _Tally) -> Run:
        return running.model_copy(
            update={
                "status": status,
                "counts": tally.snapshot(len(running.matrix)),
                "spent_usd": budget.spent_usd,
            }
        )

    async def _run_specs(
        self, run: Run, specs: Sequence[TrialSpec], budget: RunBudget, tally: _Tally
    ) -> None:
        slots = anyio.Semaphore(run.budget.concurrency)
        async with anyio.create_task_group() as group:
            for spec in specs:
                await slots.acquire()
                if budget.exceeded:
                    slots.release()
                    break
                group.start_soon(self._trial_task, run, spec, budget, tally, slots, group)

    async def _trial_task(
        self,
        run: Run,
        spec: TrialSpec,
        budget: RunBudget,
        tally: _Tally,
        slots: anyio.Semaphore,
        group: TaskGroup,
    ) -> None:
        try:
            trial, events, grades = await self._run_trial(run, spec, budget)
            # Shielded so a stored trial is always counted, even if the run is being cancelled.
            with anyio.CancelScope(shield=True):
                await self._store.append_trial_result(trial, events, grades)
                tally.add(trial.status)
        except BudgetExceededError:
            _log.warning("run.budget_exceeded", spent_usd=str(budget.spent_usd))
            group.cancel_scope.cancel()
        finally:
            slots.release()

    # --- trial level --------------------------------------------------------------------------

    async def _run_trial(
        self, run: Run, spec: TrialSpec, budget: RunBudget
    ) -> tuple[Trial, list[Event], list[Grade]]:
        scenario = self._suite.scenarios[spec.scenario_id]
        persona = self._suite.personas[spec.persona_id]
        trial_id = TrialId(new_id())
        brief = TrialBrief(
            trial_id=trial_id,
            scenario=scenario,
            persona=persona,
            channel=spec.channel,
            seed=spec.seed,
        )
        spend = TrialSpend(budget)
        context: dict[str, str | int] = {
            "trial_id": trial_id,
            "scenario_id": spec.scenario_id,
            "persona_id": spec.persona_id,
            "channel": spec.channel.value,
            "trial_index": spec.index,
        }
        with (
            bound_contextvars(**context),
            self._tracer.start_as_current_span(
                "hecklr.trial",
                attributes={f"hecklr.{key}": value for key, value in context.items()},
            ) as span,
        ):
            attempt, number = await self._attempts(brief, spend)
            trial = self._trial(run, spec, brief, attempt, number, spend)
            events = list(attempt.sink.events)
            grades = []
            if trial.status != "harness_error":
                grades = await self._grade(trial, events, scenario, persona, spend)
            if budget.exceeded:
                # A judge ran out of budget: the trial's grades are incomplete, so it is dropped.
                raise BudgetExceededError(f"budget exhausted while grading trial {trial_id}")
            trial = trial.model_copy(update={"cost_usd": spend.total_usd})
            span.set_attribute("hecklr.status", trial.status)
            span.set_attribute("hecklr.attempts", number)
            _log.info(
                "trial.end",
                status=trial.status,
                attempts=number,
                duration_ms=trial.duration_ms,
                cost_usd=str(trial.cost_usd),
            )
            return trial, events, grades

    async def _attempts(self, brief: TrialBrief, spend: TrialSpend) -> tuple[_Attempt, int]:
        number = 1
        while True:
            attempt = await self._attempt(brief, spend)
            failure = attempt.failure
            if failure is None or number == self._retry.max_attempts or not is_retryable(failure):
                return attempt, number
            delay = self._retry.delay_s(brief.seed, number)
            _log.warning(
                "trial.retry", attempt=number, delay_s=round(delay, 3), error=repr(failure)
            )
            await anyio.sleep(delay)
            number += 1

    async def _attempt(self, brief: TrialBrief, spend: TrialSpend) -> _Attempt:
        clock = self._clock_factory()
        attempt = _Attempt(sink=TrialEventSink(brief.trial_id, clock), started_at=datetime.now(UTC))
        deadline = brief.scenario.limits.max_duration_s + self._deadline_grace_s
        with anyio.move_on_after(deadline) as scope:
            try:
                await self._converse(brief, spend, attempt)
            except BudgetExceededError:
                raise
            # By contract, anything the world, simulator or driver raises is a harness failure.
            except Exception as exc:
                if spend.budget.exceeded:
                    raise BudgetExceededError("budget exhausted during the conversation") from exc
                attempt.failure = exc
                attempt.sink.emit(
                    "system",
                    ErrorPayload(
                        source="harness",
                        message=f"{type(exc).__name__}: {exc}",
                        retryable=is_retryable(exc),
                    ),
                )
        if scope.cancelled_caught:
            attempt.timed_out = True
            attempt.sink.emit(
                "system",
                ErrorPayload(
                    source="harness", message=f"trial exceeded its {deadline:g} s deadline"
                ),
            )
        attempt.duration_ms = clock.now_ms()
        return attempt

    async def _converse(self, brief: TrialBrief, spend: TrialSpend, attempt: _Attempt) -> None:
        collaborators = self._collaborators
        world = await collaborators.sandbox.open(brief, self._suite.project.world, attempt.sink)
        try:
            attempt.before = world.snapshot()
            ctx = SessionContext(
                trial_id=brief.trial_id,
                seed=brief.seed,
                world_mcp_url=world.mcp_url,
                world_http_url=world.http_url,
            )
            simulator = collaborators.simulator_factory(
                brief, MeteredProvider(collaborators.simulator_llm, spend)
            )
            attempt.simulator = simulator
            driver = collaborators.drivers[brief.channel]
            attempt.result = await driver.run(ctx, brief, simulator, attempt.sink)
            attempt.after = world.snapshot()
        finally:
            if attempt.after is None and attempt.before is not None:
                attempt.after = _snapshot_after_failure(world)
            with anyio.move_on_after(WORLD_CLOSE_TIMEOUT_S, shield=True):
                await world.close()

    def _trial(
        self,
        run: Run,
        spec: TrialSpec,
        brief: TrialBrief,
        attempt: _Attempt,
        attempts: int,
        spend: TrialSpend,
    ) -> Trial:
        status = attempt.status
        result = attempt.result
        empty = WorldSnapshot(world_id=self._suite.project.world.id, taken_at_ms=0, collections={})
        before = attempt.before or empty
        if attempt.timed_out:
            end_reason: EndReason = EndReason.MAX_DURATION
            error: str | None = "the trial exceeded its hard deadline"
        elif status == "harness_error" or result is None:
            end_reason, error = EndReason.ERROR, _describe(attempt.failure)
        else:
            end_reason, error = result.end_reason, result.error
        return Trial(
            id=brief.trial_id,
            run_id=run.id,
            scenario_id=spec.scenario_id,
            persona_id=spec.persona_id,
            channel=spec.channel,
            index=spec.index,
            seed=spec.seed,
            status=status,
            end_reason=end_reason,
            started_at=attempt.started_at,
            duration_ms=attempt.duration_ms,
            cost_usd=spend.total_usd,
            world_before=before,
            world_after=attempt.after or before,
            turns=result.turns if result is not None and not attempt.timed_out else 0,
            contradictions=attempt.simulator.contradictions if attempt.simulator else 0,
            attempts=attempts,
            error=error,
        )

    async def _grade(
        self,
        trial: Trial,
        events: Sequence[Event],
        scenario: Scenario,
        persona: Persona,
        spend: TrialSpend,
    ) -> list[Grade]:
        ctx = GradeContext(
            trial=trial,
            events=events,
            scenario=scenario,
            persona=persona,
            llm=MeteredProvider(self._collaborators.judge_llm, spend),
            judge_model=self._suite.project.config.models.judge,
        )
        return list(await self._collaborators.grade_trial(ctx))


def _snapshot_after_failure(world: WorldSession) -> WorldSnapshot | None:
    try:
        return world.snapshot()
    except Exception as exc:  # noqa: BLE001 -- the trial is already failing; keep its original error
        _log.warning("world.snapshot_failed", error=repr(exc))
        return None


def _describe(exc: BaseException | None) -> str:
    return f"{type(exc).__name__}: {exc}" if exc is not None else "unknown harness failure"
