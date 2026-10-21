from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, JsonValue, ValidationError

from hecklr_schemas import (
    AgentRef,
    AudioSpanPayload,
    Budget,
    Channel,
    Comparison,
    ComparisonId,
    EmailAttachment,
    EmailMessage,
    EmailPayload,
    EndReason,
    ErrorPayload,
    Event,
    EventId,
    EventRef,
    FailureCluster,
    FaultKind,
    Grade,
    GradeId,
    HandoffPayload,
    InterruptPayload,
    Patch,
    PatchStatus,
    Run,
    RunCounts,
    RunId,
    SilencePayload,
    ToolCallPayload,
    ToolResultPayload,
    Trial,
    TrialId,
    TrialSpec,
    UtterancePayload,
    Verdict,
    WorldSnapshot,
    new_id,
)
from hecklr_schemas.ids import ULID_PATTERN

# --- strategies -----------------------------------------------------------------------------

ulids = st.from_regex(ULID_PATTERN, fullmatch=True)
idents = st.from_regex(r"[a-z0-9][a-z0-9-]{0,20}", fullmatch=True)
ms = st.integers(min_value=0, max_value=10**9)
texts = st.text(max_size=40)
finite = st.floats(allow_nan=False, allow_infinity=False, width=64)
json_values: st.SearchStrategy[JsonValue] = st.recursive(
    st.none() | st.booleans() | st.integers(-(2**53), 2**53) | finite | texts,
    lambda children: st.lists(children, max_size=4) | st.dictionaries(texts, children, max_size=4),
    max_leaves=12,
)
json_objects = st.dictionaries(texts, json_values, max_size=4)
# Hypothesis takes naive bounds and attaches the timezone itself.
aware_datetimes = st.datetimes(
    min_value=datetime(2000, 1, 1),  # noqa: DTZ001
    max_value=datetime(2100, 1, 1),  # noqa: DTZ001
    timezones=st.just(UTC),
)
money = st.decimals(min_value=0, max_value=10_000, places=6, allow_nan=False)
addresses = st.from_regex(r"[a-z]{1,8}@[a-z]{1,8}\.test", fullmatch=True)


@st.composite
def spans(draw: st.DrawFn) -> tuple[int, int]:
    start = draw(ms)
    return start, start + draw(st.integers(0, 60_000))


@st.composite
def audio_spans(draw: st.DrawFn) -> AudioSpanPayload:
    start, end = draw(spans())
    return AudioSpanPayload(
        start_ms=start,
        end_ms=end,
        content=draw(st.sampled_from(["speech", "backchannel", "filler", "noise"])),
        start_sample=draw(st.none() | st.integers(0, 10**9)),
        sample_rate=draw(st.none() | st.sampled_from([8000, 16000, 48000])),
    )


@st.composite
def silences(draw: st.DrawFn) -> SilencePayload:
    start, end = draw(spans())
    return SilencePayload(
        start_ms=start, end_ms=end, attributed_to=draw(st.sampled_from([None, "user", "agent"]))
    )


@st.composite
def tool_results(draw: st.DrawFn) -> ToolResultPayload:
    failed = draw(st.booleans())
    return ToolResultPayload(
        call_id=draw(texts),
        tool=draw(idents),
        result=None if failed else draw(json_values),
        error=draw(texts) if failed else None,
        latency_ms=draw(ms),
        fault=draw(st.none() | st.sampled_from(list(FaultKind))),
    )


emails = st.builds(
    EmailMessage,
    message_id=st.from_regex(r"<[a-z0-9]{4,12}@hecklr\.test>", fullmatch=True),
    in_reply_to=st.none() | texts,
    references=st.lists(texts, max_size=3),
    subject=texts,
    body=texts,
    from_addr=addresses,
    to_addrs=st.lists(addresses, min_size=1, max_size=3),
    attachments=st.lists(
        st.builds(EmailAttachment, filename=idents, data=st.binary(max_size=64)), max_size=2
    ),
)

payloads = st.one_of(
    st.builds(
        UtterancePayload,
        text=texts,
        turn=st.integers(0, 500),
        interrupted=st.booleans(),
        asr_confidence=st.none() | st.floats(0, 1),
    ),
    audio_spans(),
    st.builds(
        ToolCallPayload,
        call_id=texts,
        tool=idents,
        args=json_objects,
        source=st.sampled_from(["sandbox", "observed"]),
    ),
    tool_results(),
    st.builds(
        InterruptPayload,
        target=st.sampled_from(["user", "agent"]),
        style=st.sampled_from(["barge_in", "backchannel"]),
    ),
    silences(),
    st.builds(EmailPayload, message=emails),
    st.builds(HandoffPayload, reason=st.none() | texts, target=st.none() | texts),
    st.builds(
        ErrorPayload,
        source=st.sampled_from(["agent", "harness", "simulator", "world"]),
        message=texts,
        retryable=st.booleans(),
    ),
)

events = st.builds(
    Event,
    id=ulids.map(EventId),
    trial_id=ulids.map(TrialId),
    seq=st.integers(0, 10**6),
    t_ms=ms,
    actor=st.sampled_from(["user", "agent", "tool", "system"]),
    payload=payloads,
)

snapshots = st.builds(
    WorldSnapshot,
    world_id=idents,
    taken_at_ms=ms,
    collections=st.dictionaries(
        idents, st.dictionaries(idents, json_objects, max_size=3), max_size=3
    ),
)

trials = st.builds(
    Trial,
    id=ulids.map(TrialId),
    run_id=ulids.map(RunId),
    scenario_id=idents,
    persona_id=idents,
    channel=st.sampled_from(list(Channel)),
    index=st.integers(0, 100),
    seed=st.integers(-(2**63), 2**63 - 1),
    status=st.sampled_from(["ok", "agent_error", "harness_error", "timeout"]),
    end_reason=st.none() | st.sampled_from(list(EndReason)),
    started_at=aware_datetimes,
    duration_ms=ms,
    cost_usd=money,
    world_before=snapshots,
    world_after=snapshots,
    turns=st.integers(0, 100),
    contradictions=st.integers(0, 10),
    attempts=st.integers(1, 3),
    error=st.none() | texts,
)

grades = st.builds(
    Grade,
    id=ulids.map(GradeId),
    trial_id=ulids.map(TrialId),
    grader=idents,
    grader_version=idents,
    criterion_index=st.none() | st.integers(0, 20),
    status=st.just("ok"),
    value=st.booleans() | finite,
    passed=st.booleans(),
    rationale=st.none() | texts,
    evidence=st.lists(
        st.builds(EventRef, trial_id=ulids.map(TrialId), event_id=ulids.map(EventId)), max_size=3
    ),
    judge_model=st.none() | st.just("anthropic/claude-sonnet-5-5"),
    cost_usd=money,
)

specs = st.builds(
    TrialSpec,
    scenario_id=idents,
    persona_id=idents,
    channel=st.sampled_from(list(Channel)),
    index=st.integers(0, 100),
    seed=st.integers(0, 2**32),
)

runs = st.builds(
    Run,
    id=ulids.map(RunId),
    created_at=aware_datetimes,
    git_sha=st.none() | st.from_regex(r"[0-9a-f]{40}", fullmatch=True),
    agent=st.builds(AgentRef, name=idents, version=st.none() | texts),
    config_hash=st.from_regex(r"[0-9a-f]{64}", fullmatch=True),
    seed=st.integers(0, 2**32),
    suite=st.none() | idents,
    matrix=st.lists(specs, max_size=5),
    budget=st.builds(Budget, usd=st.none() | money, concurrency=st.integers(1, 256)),
    counts=st.builds(
        RunCounts,
        planned=st.integers(0, 100),
        ok=st.integers(0, 100),
        agent_error=st.integers(0, 100),
        harness_error=st.integers(0, 100),
        timeout=st.integers(0, 100),
    ),
    spent_usd=money,
)


def _round_trips[M: BaseModel](model: M) -> None:
    restored = type(model).model_validate_json(model.model_dump_json())
    assert restored == model


# --- round trips ----------------------------------------------------------------------------


@settings(max_examples=300)
@given(events)
def test_events_round_trip_through_json(event: Event) -> None:
    _round_trips(event)
    assert event.kind == event.payload.kind


@given(trials)
def test_trials_round_trip_through_json(trial: Trial) -> None:
    _round_trips(trial)
    assert trial.spec.key == (trial.scenario_id, trial.persona_id, trial.channel, trial.index)


@given(grades)
def test_grades_round_trip_through_json(grade: Grade) -> None:
    _round_trips(grade)


@given(runs)
def test_runs_round_trip_through_json(run: Run) -> None:
    _round_trips(run)


# --- invariants -----------------------------------------------------------------------------


def _event(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": new_id(),
        "trial_id": new_id(),
        "seq": 0,
        "t_ms": 0,
        "actor": "agent",
        "payload": payload,
    }


def test_event_payload_is_chosen_by_kind() -> None:
    event = Event.model_validate(
        _event({"kind": "tool_call", "call_id": "c1", "tool": "get_customer", "args": {"id": 1}})
    )

    assert isinstance(event.payload, ToolCallPayload)
    assert event.kind == "tool_call"


def test_event_with_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        Event.model_validate(_event({"kind": "telepathy"}))


def test_event_times_cannot_be_negative() -> None:
    data = _event({"kind": "utterance", "text": "hi", "turn": 0})
    data["t_ms"] = -1

    with pytest.raises(ValidationError):
        Event.model_validate(data)


@pytest.mark.parametrize("payload_type", [AudioSpanPayload, SilencePayload])
def test_spans_cannot_end_before_they_start(payload_type: type[BaseModel]) -> None:
    with pytest.raises(ValidationError, match="precedes"):
        payload_type.model_validate({"start_ms": 500, "end_ms": 499})


def test_tool_result_is_result_xor_error() -> None:
    with pytest.raises(ValidationError, match="not both"):
        ToolResultPayload(call_id="c", tool="t", result={"ok": True}, error="boom", latency_ms=3)

    failed = ToolResultPayload(call_id="c", tool="t", error="boom", latency_ms=3)
    assert not failed.ok
    assert ToolResultPayload(call_id="c", tool="t", result=None, latency_ms=3).ok


def test_grade_in_error_status_cannot_pass() -> None:
    with pytest.raises(ValidationError, match="cannot pass"):
        Grade(
            id=GradeId(new_id()),
            trial_id=TrialId(new_id()),
            grader="judge:resolved",
            grader_version="1",
            status="error",
            value=False,
            passed=True,
        )


def test_grade_value_must_be_finite() -> None:
    with pytest.raises(ValidationError, match="finite"):
        Grade(
            id=GradeId(new_id()),
            trial_id=TrialId(new_id()),
            grader="response_latency",
            grader_version="1",
            value=float("nan"),
            passed=False,
        )


def _comparison(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": ComparisonId(new_id()),
        "baseline": RunId(new_id()),
        "candidate": RunId(new_id()),
        "metric": "pass_rate",
        "baseline_value": 0.9,
        "candidate_value": 0.83,
        "delta": -0.07,
        "ci_low": -0.11,
        "ci_high": -0.03,
        "p_value": 0.004,
        "q_value": 0.01,
        "verdict": Verdict.REGRESSION,
        "alpha": 0.05,
        "min_effect": 0.03,
        "n_scenarios": 40,
        "n_trials_baseline": 120,
        "n_trials_candidate": 120,
        "created_at": datetime(2026, 10, 21, tzinfo=UTC),
    }
    data.update(overrides)
    return data


def test_comparison_round_trips_and_defaults_to_suite_slice() -> None:
    comparison = Comparison.model_validate(_comparison())

    assert comparison.slice == {}
    assert comparison.confidence == 0.95
    _round_trips(comparison)


def test_comparison_interval_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="exceeds ci_high"):
        Comparison.model_validate(_comparison(ci_low=0.1, ci_high=-0.1))


def test_trials_needed_only_for_inconclusive() -> None:
    with pytest.raises(ValidationError, match="inconclusive"):
        Comparison.model_validate(_comparison(trials_needed=12))

    inconclusive = Comparison.model_validate(
        _comparison(verdict="inconclusive", trials_needed=12, p_value=0.2, q_value=0.3)
    )
    assert inconclusive.trials_needed == 12


@pytest.mark.parametrize("field", ["p_value", "q_value"])
def test_probabilities_stay_in_unit_interval(field: str) -> None:
    with pytest.raises(ValidationError):
        Comparison.model_validate(_comparison(**{field: 1.2}))


def test_run_timestamps_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        Run(
            id=RunId(new_id()),
            created_at=datetime(2026, 10, 21),  # noqa: DTZ001 -- the naive value under test
            agent=AgentRef(name="acme"),
            config_hash="0" * 64,
            seed=1,
            matrix=[],
            budget=Budget(),
        )


def test_run_counts_report_finished_trials() -> None:
    counts = RunCounts(planned=10, ok=5, agent_error=1, harness_error=2, timeout=1)

    assert counts.finished == 9


def test_failure_cluster_size_counts_both_sources() -> None:
    cluster = FailureCluster(
        id=new_id(),  # type: ignore[arg-type]  # pydantic validates the str at runtime
        label="fails when the customer gives two addresses",
        source="production",
        trial_ids=[TrialId(new_id())],
        conversation_ids=["prod-1", "prod-2"],
        created_at=datetime(2026, 10, 21, tzinfo=UTC),
    )

    assert cluster.size == 3
    _round_trips(cluster)


def test_patch_round_trips_with_validation_results() -> None:
    patch = Patch(
        id=new_id(),  # type: ignore[arg-type]  # pydantic validates the str at runtime
        cluster_id=new_id(),  # type: ignore[arg-type]  # pydantic validates the str at runtime
        candidate_index=0,
        files=["prompts/system.md"],
        diff="--- a/prompts/system.md\n+++ b/prompts/system.md\n@@ -1 +1 @@\n-a\n+b\n",
        rationale="Confirm the slot before booking.",
        status=PatchStatus.ACCEPTED,
        targeted=[Comparison.model_validate(_comparison(verdict="improvement", delta=0.1))],
        cost_usd=Decimal("0.42"),
        created_at=datetime(2026, 10, 21, tzinfo=UTC),
    )

    _round_trips(patch)


def test_patch_must_touch_a_file() -> None:
    with pytest.raises(ValidationError):
        Patch.model_validate(
            {
                "id": new_id(),
                "cluster_id": new_id(),
                "candidate_index": 0,
                "files": [],
                "diff": "",
                "rationale": "",
                "created_at": "2026-10-21T00:00:00Z",
            }
        )
