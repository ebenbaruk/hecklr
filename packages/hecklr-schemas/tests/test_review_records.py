"""Statistics, labels, proposals, transcripts and the voice-metric event fields."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from hecklr_schemas import (
    Conversation,
    Estimate,
    Event,
    EventId,
    FailureCluster,
    HecklrConfig,
    HumanLabel,
    InterruptPayload,
    IntervalMethod,
    MetricSummary,
    ModelPrice,
    ProposalStatus,
    RunId,
    Scenario,
    ScenarioProposal,
    ScenarioSource,
    SilencePayload,
    ToolFault,
    TrialId,
    UtterancePayload,
    new_id,
)

NOW = datetime(2026, 10, 21, tzinfo=UTC)


def _round_trips(model: BaseModel) -> None:
    assert type(model).model_validate_json(model.model_dump_json()) == model


# --- estimates ------------------------------------------------------------------------------


def test_metric_summary_carries_an_interval() -> None:
    summary = MetricSummary(
        run_id=RunId(new_id()),
        metric="pass_rate",
        slice={"scenario": "reschedule-boiler-visit"},
        estimate=Estimate(value=0.8, ci_low=0.49, ci_high=0.94, n=10, method=IntervalMethod.WILSON),
    )

    assert summary.estimate.confidence == 0.95
    _round_trips(summary)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"ci_low": 0.9, "ci_high": 0.1}, "exceeds ci_high"),
        ({"value": float("nan")}, "finite"),
        ({"ci_high": float("inf")}, "finite"),
    ],
)
def test_estimate_rejects_broken_intervals(fields: dict[str, float], message: str) -> None:
    data: dict[str, Any] = {"value": 0.5, "ci_low": 0.4, "ci_high": 0.6, "n": 5, "method": "wilson"}
    with pytest.raises(ValidationError, match=message):
        Estimate.model_validate(data | fields)


# --- labels and proposals -------------------------------------------------------------------


def test_human_label_pins_the_rubric_version() -> None:
    label = HumanLabel(
        id=new_id(),  # type: ignore[arg-type]  # pydantic validates the str at runtime
        trial_id=TrialId(new_id()),
        grade_id=new_id(),  # type: ignore[arg-type]  # pydantic validates the str at runtime
        grader="judge:resolved",
        grader_version="3",
        passed=False,
        labeler="maya",
        created_at=NOW,
    )

    _round_trips(label)
    with pytest.raises(ValidationError):
        HumanLabel.model_validate(label.model_dump() | {"labeler": ""})


def _proposal(load_yaml: Any, **fields: Any) -> dict[str, Any]:
    return {
        "id": new_id(),
        "cluster_id": new_id(),
        "scenario": load_yaml("spec_scenario.yaml"),
        "created_at": NOW,
        **fields,
    }


def test_scenario_proposal_starts_undecided_and_records_the_decision(load_yaml: Any) -> None:
    proposed = ScenarioProposal.model_validate(_proposal(load_yaml))
    approved = ScenarioProposal.model_validate(
        _proposal(load_yaml, status="approved", reviewer="maya", decided_at=NOW)
    )

    assert proposed.status is ProposalStatus.PROPOSED
    assert isinstance(approved.scenario, Scenario)
    _round_trips(approved)


@pytest.mark.parametrize(
    "fields",
    [{"status": "approved"}, {"status": "rejected", "reviewer": "maya"}, {"reviewer": "maya"}],
)
def test_proposal_decision_fields_go_together(load_yaml: Any, fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="decided"):
        ScenarioProposal.model_validate(_proposal(load_yaml, **fields))


# --- clusters and provenance ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"source": "run", "conversation_ids": ["prod-1"]}, "conversation ids"),
        ({"source": "production", "trial_ids": [new_id()]}, "trial ids"),
        ({"source": "run", "exemplar_trial_ids": [new_id()]}, "members"),
        (
            {"source": "production", "conversation_ids": ["a"], "exemplar_conversation_ids": ["b"]},
            "members",
        ),
    ],
)
def test_cluster_members_match_its_source(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        FailureCluster.model_validate({"id": new_id(), "label": "x", "created_at": NOW} | fields)


def test_scenario_source_cluster_id_is_a_ulid() -> None:
    with pytest.raises(ValidationError):
        ScenarioSource(kind="mined", cluster_id="cluster-7")  # type: ignore[arg-type]
    assert ScenarioSource(kind="mined", cluster_id=new_id()).cluster_id  # type: ignore[arg-type]


# --- transcripts ----------------------------------------------------------------------------


def _conversation(**fields: Any) -> dict[str, Any]:
    return {
        "id": "zendesk-88123",
        "started_at": "2026-10-20T09:15:00Z",
        "turns": [
            {"role": "user", "text": "I need to move my boiler visit", "t_ms": 0},
            {
                "role": "agent",
                "text": "Sure, which day?",
                "t_ms": 1800,
                "tool_calls": [{"tool": "find_slots", "args": {"postcode": "92000"}}],
            },
        ],
        "metadata": {"escalated": False},
        **fields,
    }


def test_native_jsonl_line_parses_into_a_conversation() -> None:
    conversation = Conversation.model_validate(_conversation())

    assert conversation.source == "jsonl"
    assert conversation.turns[1].tool_calls[0].tool == "find_slots"
    _round_trips(conversation)


@pytest.mark.parametrize(
    ("turns", "message"),
    [
        (
            [{"role": "user", "text": "a", "t_ms": 5}, {"role": "agent", "text": "b", "t_ms": 1}],
            "back",
        ),
        ([{"role": "user", "text": "a", "tool_calls": [{"tool": "x"}]}], "agent turns"),
        ([], "at least 1"),
    ],
)
def test_conversation_rejects_inconsistent_turns(turns: list[Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Conversation.model_validate(_conversation(turns=turns))


# --- voice and chat timing fields -----------------------------------------------------------


def _event(payload: dict[str, Any], t_ms: int = 1000) -> dict[str, Any]:
    return {
        "id": EventId(new_id()),
        "trial_id": TrialId(new_id()),
        "seq": 0,
        "t_ms": t_ms,
        "actor": "agent",
        "payload": payload,
    }


def test_utterance_cannot_end_before_it_starts() -> None:
    with pytest.raises(ValidationError, match="precedes t_ms"):
        Event.model_validate(_event({"kind": "utterance", "text": "hi", "turn": 1, "end_ms": 0}))
    ok = Event.model_validate(
        _event({"kind": "utterance", "text": "hi", "turn": 1, "end_ms": 1000})
    )
    assert isinstance(ok.payload, UtterancePayload)


def test_planned_silences_belong_to_the_caller() -> None:
    hesitation = SilencePayload(start_ms=0, end_ms=700, attributed_to="user", reason="hesitation")

    assert hesitation.reason == "hesitation"
    assert SilencePayload(start_ms=0, end_ms=2500, attributed_to="agent").reason == "turn_gap"
    with pytest.raises(ValidationError, match="attributed_to must be user"):
        SilencePayload(start_ms=0, end_ms=6000, attributed_to="agent", reason="re_engagement")


def test_interrupt_reaction_is_optional() -> None:
    kept_talking = InterruptPayload(target="agent", style="backchannel")
    stopped = InterruptPayload(target="agent", reaction_ms=240)

    assert kept_talking.reaction_ms is None
    assert stopped.reaction_ms == 240


# --- faults and config ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "fault",
    [
        {"tool": "t", "kind": "timeout", "delay_ms": 5},
        {"tool": "t", "kind": "http_500", "patch": {"fee": 0}},
        {"tool": "t", "kind": "slow", "delay_ms": 5, "patch": {"fee": 0}},
    ],
)
def test_tool_fault_rejects_parameters_of_other_kinds(fault: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="only allowed"):
        ToolFault.model_validate(fault)


def test_config_declares_tool_mode_and_model_prices(load_yaml: Any) -> None:
    data = load_yaml("spec_hecklr.yaml")
    data["agent"]["tool_mode"] = "observed"
    data["models"]["prices"] = {
        "openai-compatible/llama-70b": {"input_per_mtok": "0", "output_per_mtok": "0"},
        "openai/gpt-5": {"input_per_mtok": "1.25", "output_per_mtok": "10"},
    }

    config = HecklrConfig.model_validate(data)

    assert config.agent.tool_mode == "observed"
    assert config.models.prices["openai/gpt-5"] == ModelPrice(
        input_per_mtok=Decimal("1.25"), output_per_mtok=Decimal(10)
    )
    assert HecklrConfig.model_validate(load_yaml("spec_hecklr.yaml")).agent.tool_mode == "sandbox"
