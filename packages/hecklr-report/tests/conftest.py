"""Synthetic stored runs built from the real schema models, for every report test."""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hecklr_core import RunResults
from hecklr_report import Analysis, analyze
from hecklr_schemas import (
    AgentRef,
    AudioSpanPayload,
    Budget,
    Channel,
    Event,
    EventId,
    EventPayload,
    EventRef,
    Grade,
    GradeId,
    InterruptPayload,
    Persona,
    Run,
    RunId,
    RunStatus,
    Scenario,
    SilencePayload,
    StatsConfig,
    ToolCallPayload,
    ToolResultPayload,
    Trial,
    TrialId,
    TrialStatus,
    TtsConfig,
    UtterancePayload,
    VoiceProfile,
    WorldSnapshot,
    new_id,
)
from hecklr_schemas.common import Actor

FAST_STATS = StatsConfig(bootstrap_resamples=1_000, seed=11)
"""Fewer resamples keep tests fast; a fixed seed keeps them deterministic."""

WORLD_BEFORE = WorldSnapshot(
    world_id="acme-maintenance",
    taken_at_ms=0,
    collections={"appointments": {"C-1042": {"customer_id": "C-1042", "slot": "2026-10-23T09:00"}}},
)
WORLD_AFTER = WorldSnapshot(
    world_id="acme-maintenance",
    taken_at_ms=9_000,
    collections={
        "appointments": {"C-1042": {"customer_id": "C-1042", "slot": "2026-10-28T09:00"}},
        "notes": {"N-1": {"text": "moved"}},
    },
)


def make_scenario(scenario_id: str, *, tags: Sequence[str] = ("core",)) -> Scenario:
    """A scenario with a tool criterion (index 0) and a judge criterion (index 1)."""
    return Scenario.model_validate(
        {
            "id": scenario_id,
            "title": f"Title of {scenario_id}",
            "channels": ["chat", "voice"],
            "tags": list(tags),
            "persona": "busy-homeowner",
            "world": "acme-maintenance",
            "goal": "Move the visit",
            "success": [
                {"type": "tool_called", "tool": "reschedule_appointment"},
                {"type": "judge", "rubric": "resolved"},
            ],
        }
    )


def make_persona(
    persona_id: str, *, snr_db: float | None = None, accent: str | None = None
) -> Persona:
    """A persona, with a voice profile when ``snr_db`` or ``accent`` is given."""
    voice = None
    if snr_db is not None or accent is not None:
        voice = VoiceProfile(
            tts=TtsConfig(provider="kokoro", voice="af"),
            accent=accent,
            environment="street" if snr_db is not None else None,
            snr_db=snr_db,
        )
    return Persona(id=persona_id, description="A customer", voice=voice)


class TrialLog:
    """Appends events to one trial with ids and ``seq`` assigned like the real sink."""

    def __init__(self, trial_id: TrialId) -> None:
        self.trial_id = trial_id
        self.events: list[Event] = []

    def add(self, actor: Actor, payload: EventPayload, t_ms: int) -> Event:
        event = Event(
            id=EventId(new_id()),
            trial_id=self.trial_id,
            seq=len(self.events),
            t_ms=t_ms,
            actor=actor,
            payload=payload,
        )
        self.events.append(event)
        return event


class RunBuilder:
    """Builds a stored run trial by trial."""

    def __init__(
        self, *, seed: int = 1, suite: str = "ci", status: RunStatus = RunStatus.COMPLETED
    ):
        self.run = Run(
            id=RunId(new_id()),
            created_at=datetime(2026, 10, 21, 12, 0, tzinfo=UTC),
            agent=AgentRef(name="acme-support", version="v2"),
            config_hash="0" * 64,
            seed=seed,
            suite=suite,
            matrix=[],
            budget=Budget(),
            status=status,
            spent_usd=Decimal("0.42"),
            judge_model="fake/judge",
        )
        self.trials: list[Trial] = []
        self.events: list[Event] = []
        self.grades: list[Grade] = []

    def trial(
        self,
        scenario_id: str,
        *,
        passed: bool = True,
        index: int | None = None,
        persona_id: str = "busy-homeowner",
        channel: Channel = Channel.CHAT,
        status: TrialStatus = "ok",
        latencies_ms: Sequence[int] = (400, 600),
        judge_error: bool = False,
        tool_failed: bool = False,
        rationale: str = "The agent never confirmed the new slot.",
        user_text: str = "Can you move my visit?",
        contradictions: int = 0,
        voice: VoiceShape | None = None,
    ) -> Trial:
        """Add one trial: a short conversation, a tool call, and two criterion grades."""
        trial_id = TrialId(new_id())
        log = TrialLog(trial_id)
        t = 0
        evidence = []
        for turn, latency in enumerate(latencies_ms):
            log.add("user", UtterancePayload(text=user_text, turn=2 * turn, end_ms=t + 500), t)
            t += 500 + latency
            evidence.append(
                log.add(
                    "agent",
                    UtterancePayload(
                        text=f"Reply {turn}", turn=2 * turn + 1, end_ms=t + 800, latency_ms=latency
                    ),
                    t,
                )
            )
            t += 800
        log.add(
            "agent",
            ToolCallPayload(call_id="c1", tool="reschedule_appointment", args={"id": "C-1042"}),
            t,
        )
        log.add(
            "tool",
            ToolResultPayload(
                call_id="c1", tool="reschedule_appointment", result={"ok": True}, latency_ms=40
            ),
            t + 40,
        )
        if voice is not None:
            voice.write(log, t + 100)
        if index is None:
            index = sum(
                1
                for x in self.trials
                if x.scenario_id == scenario_id
                and x.persona_id == persona_id
                and x.channel == channel
            )
        trial = Trial(
            id=trial_id,
            run_id=self.run.id,
            scenario_id=scenario_id,
            persona_id=persona_id,
            channel=channel,
            index=index,
            seed=random.Random(index).getrandbits(32),
            status=status,
            started_at=self.run.created_at,
            duration_ms=max(t + 2_000, 1),
            cost_usd=Decimal("0.01"),
            world_before=WORLD_BEFORE,
            world_after=WORLD_AFTER,
            turns=2 * len(latencies_ms),
            contradictions=contradictions,
            error="agent returned HTTP 500" if status == "agent_error" else None,
        )
        self.trials.append(trial)
        self.events.extend(log.events)
        if status != "harness_error":
            self.grades.append(self._grade(trial_id, "tool_called", 0, passed=not tool_failed))
            judge_passed = passed and not tool_failed
            self.grades.append(
                self._grade(
                    trial_id,
                    "judge:resolved",
                    1,
                    passed=judge_passed if not judge_error else False,
                    status="error" if judge_error else "ok",
                    rationale=None if judge_passed else rationale,
                    evidence=[EventRef(trial_id=trial_id, event_id=e.id) for e in evidence[:1]],
                )
            )
        return trial

    def _grade(
        self,
        trial_id: TrialId,
        grader: str,
        index: int,
        *,
        passed: bool,
        status: str = "ok",
        rationale: str | None = None,
        evidence: list[EventRef] | None = None,
    ) -> Grade:
        return Grade.model_validate(
            {
                "id": GradeId(new_id()),
                "trial_id": trial_id,
                "grader": grader,
                "grader_version": "abc123",
                "criterion_index": index,
                "status": status,
                "value": passed,
                "passed": passed,
                "rationale": rationale,
                "evidence": evidence or [],
            }
        )

    def suite(
        self, rates: dict[str, float], trials: int, *, rng_seed: int = 0, **kwargs: object
    ) -> None:
        """Add ``trials`` trials per scenario, each passing with the scenario's rate (seeded)."""
        rng = random.Random(rng_seed)
        for scenario_id, rate in rates.items():
            for _ in range(trials):
                self.trial(scenario_id, passed=rng.random() < rate, **kwargs)  # type: ignore[arg-type]

    def build(self) -> RunResults:
        return RunResults(run=self.run, trials=self.trials, events=self.events, grades=self.grades)


class VoiceShape:
    """Voice turn-taking events appended to a trial: speech spans, a barge-in, a hesitation."""

    def __init__(
        self,
        *,
        barge_in_reaction_ms: int | None = 200,
        recording: str | None = None,
        dead_air_ms: int = 0,
    ) -> None:
        self.barge_in_reaction_ms = barge_in_reaction_ms
        self.recording = recording
        self.dead_air_ms = dead_air_ms

    def write(self, log: TrialLog, t: int) -> None:
        log.add("user", AudioSpanPayload(start_ms=t, end_ms=t + 900, recording=self.recording), t)
        agent_start = t + 1_300
        log.add(
            "agent",
            AudioSpanPayload(
                start_ms=agent_start, end_ms=agent_start + 2_000, recording=self.recording
            ),
            agent_start,
        )
        log.add(
            "user",
            InterruptPayload(target="agent", reaction_ms=self.barge_in_reaction_ms),
            agent_start + 1_000,
        )
        log.add(
            "system",
            SilencePayload(
                start_ms=t + 900, end_ms=t + 1_300, attributed_to="user", reason="hesitation"
            ),
            t + 900,
        )
        if self.dead_air_ms:
            end = agent_start + 2_000
            log.add(
                "system",
                SilencePayload(start_ms=end, end_ms=end + self.dead_air_ms, attributed_to="agent"),
                end,
            )


class Kit:
    """The builders, handed to tests as a fixture (importlib mode cannot import conftest)."""

    RunBuilder = RunBuilder
    VoiceShape = VoiceShape
    TrialLog = TrialLog
    stats = FAST_STATS

    @staticmethod
    def scenario(scenario_id: str, *, tags: Sequence[str] = ("core",)) -> Scenario:
        return make_scenario(scenario_id, tags=tags)

    @staticmethod
    def persona(
        persona_id: str, *, snr_db: float | None = None, accent: str | None = None
    ) -> Persona:
        return make_persona(persona_id, snr_db=snr_db, accent=accent)


@pytest.fixture(scope="session")
def kit() -> type[Kit]:
    return Kit


@pytest.fixture(scope="session")
def scenarios() -> dict[str, Scenario]:
    return {
        f"s{i:02d}": make_scenario(f"s{i:02d}", tags=["core", "billing" if i % 2 else "scheduling"])
        for i in range(40)
    }


@pytest.fixture(scope="session")
def regression(kit: type[Kit], scenarios: dict[str, Scenario]) -> tuple[Analysis, RunResults]:
    """A candidate that broke 12 of 40 scenarios, analyzed against its baseline."""
    baseline = RunBuilder(seed=1)
    baseline.suite({f"s{i:02d}": 0.9 for i in range(40)}, 5, rng_seed=1)
    candidate = RunBuilder(seed=2)
    candidate.suite({f"s{i:02d}": 0.1 if i < 12 else 0.9 for i in range(40)}, 5, rng_seed=2)
    results = candidate.build()
    return analyze(results, baseline.build(), FAST_STATS, scenarios), results
