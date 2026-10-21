from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from hecklr_schemas import (
    Behaviors,
    Channel,
    CollectionOp,
    FaultKind,
    HecklrConfig,
    HttpAdapterConfig,
    JudgeCriterion,
    LineCodec,
    Persona,
    PipecatAdapterConfig,
    PythonOp,
    Scenario,
    SimulationMode,
    SmtpImapAdapterConfig,
    StateCriterion,
    SuccessCriterion,
    ToolCalledCriterion,
    ToolFault,
    World,
)

LoadYaml = Callable[[str], dict[str, Any]]
_criterion = TypeAdapter[SuccessCriterion](SuccessCriterion)


def _minimal_scenario(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "s1",
        "title": "t",
        "channels": ["chat"],
        "persona": "p1",
        "world": "w1",
        "goal": "g",
        "success": [{"type": "contains", "text": "done"}],
    }
    data.update(overrides)
    return data


# --- spec examples parse verbatim ------------------------------------------------------------


def test_spec_scenario_yaml_parses_verbatim(load_yaml: LoadYaml) -> None:
    scenario = Scenario.model_validate(load_yaml("spec_scenario.yaml"))

    assert scenario.id == "reschedule-boiler-visit"
    assert scenario.channels == [Channel.VOICE, Channel.CHAT, Channel.EMAIL]
    assert scenario.persona_ids == ["busy-homeowner"]
    assert scenario.facts == {
        "customer_id": "C-1042",
        "postcode": "92000",
        "current_slot": "2026-10-23T09:00",
    }
    assert [c.type for c in scenario.success] == ["tool_called", "state", "judge", "judge"]
    tool_called = scenario.success[0]
    assert isinstance(tool_called, ToolCalledCriterion)
    assert tool_called.args == {"customer_id": "C-1042"}
    assert isinstance(scenario.success[1], StateCriterion)
    assert "weekday < 5" in scenario.success[1].expr
    assert isinstance(scenario.success[3], JudgeCriterion)
    assert scenario.success[3].rubric == "no_hallucination"
    assert scenario.limits.max_turns == 24
    assert scenario.limits.max_duration_s == 300
    assert scenario.source is not None
    assert scenario.source.kind == "replay"
    assert scenario.source.conversation_id == "prod-88213"
    assert scenario.mode is SimulationMode.GUIDED
    assert scenario.schema_version == 1


def test_spec_persona_yaml_parses_verbatim(load_yaml: LoadYaml) -> None:
    persona = Persona.model_validate(load_yaml("spec_persona.yaml"))

    assert persona.id == "busy-homeowner"
    assert persona.language == "fr-FR"
    assert persona.traits.patience == 0.4
    assert persona.behaviors.interrupt_prob == 0.25
    assert persona.behaviors.mid_utterance_pause_s == (0.4, 1.2)
    assert persona.behaviors.fillers is True
    assert persona.voice is not None
    assert persona.voice.tts.provider == "cartesia"
    assert persona.voice.tts.rate == 1.1
    assert persona.voice.environment == "car_interior"
    assert persona.voice.snr_db == 12
    assert persona.voice.line is LineCodec.G711_MULAW_8K
    assert persona.voice.packet_loss == 0.02
    assert persona.adversarial is None


def test_spec_hecklr_yaml_parses_verbatim(load_yaml: LoadYaml) -> None:
    config = HecklrConfig.model_validate(load_yaml("spec_hecklr.yaml"))

    adapters = config.agent.adapters
    assert isinstance(adapters.chat, HttpAdapterConfig)
    assert adapters.chat.stream == "sse"
    assert str(adapters.chat.url) == "http://localhost:8000/chat"
    assert isinstance(adapters.voice, PipecatAdapterConfig)
    assert isinstance(adapters.email, SmtpImapAdapterConfig)
    assert adapters.email.inbox == "support@acme.test"
    assert adapters.channels() == [Channel.CHAT, Channel.VOICE, Channel.EMAIL]
    assert config.agent.editable == ["prompts/system.md", "tools/descriptions.yaml"]
    assert set(config.suites) == {"ci", "nightly", "redteam"}
    assert config.suites["ci"].tags == ["core"]
    assert config.suites["ci"].channels == [Channel.CHAT]
    assert config.suites["nightly"].adaptive is True
    assert config.suites["redteam"].personas == "personas/adversarial/*.yaml"
    assert config.suites["redteam"].scenarios == "scenarios/**/*.yaml"
    assert config.models.simulator == "anthropic/claude-sonnet-5-5"
    assert config.models.judge == "openai/gpt-4.1"
    assert config.stats.min_effect == 0.03
    assert config.stats.bootstrap_resamples == 10_000
    assert config.budget.usd == Decimal(25)
    assert config.budget.concurrency == 32
    assert config.baseline.store == "github-artifacts"
    assert config.gate.on_inconclusive == "fail"


def test_reference_world_parses(load_yaml: LoadYaml) -> None:
    world = World.model_validate(load_yaml("acme_world.yaml"))

    assert set(world.collections) >= {"customers", "appointments", "refunds", "policies"}
    assert world.collections["appointments"].key == "customer_id"
    tools = {tool.name: tool for tool in world.tools}
    assert isinstance(tools["reschedule_appointment"].op, CollectionOp)
    assert tools["reschedule_appointment"].op.kind == "update"
    assert isinstance(tools["escalate_emergency"].op, PythonOp)
    assert tools["escalate_emergency"].params == {"type": "object", "properties": {}}


# --- typos and wrong shapes fail fast --------------------------------------------------------


def test_unknown_scenario_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="succes"):
        Scenario.model_validate(_minimal_scenario(succes=[]))


def test_unknown_field_inside_a_criterion_is_rejected() -> None:
    with pytest.raises(ValidationError, match="tol"):
        _criterion.validate_python({"type": "tool_called", "tool": "x", "tol": "y"})


def test_unknown_criterion_type_names_the_discriminator() -> None:
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        _criterion.validate_python({"type": "tool_caled", "tool": "x"})


def test_unknown_config_field_is_rejected(load_yaml: LoadYaml) -> None:
    data = load_yaml("spec_hecklr.yaml")
    data["budgett"] = {"usd": 1}

    with pytest.raises(ValidationError, match="budgett"):
        HecklrConfig.model_validate(data)


def test_adapter_type_must_match_its_channel() -> None:
    with pytest.raises(ValidationError):
        HecklrConfig.model_validate(
            {
                "version": 1,
                "agent": {"name": "a", "adapters": {"voice": {"type": "http", "url": "http://x"}}},
                "world": "w.yaml",
                "suites": {"ci": {}},
                "models": {"simulator": "fake/sim", "judge": "fake/judge"},
            }
        )


def test_agent_needs_at_least_one_adapter() -> None:
    with pytest.raises(ValidationError, match="at least one channel"):
        HecklrConfig.model_validate(
            {
                "version": 1,
                "agent": {"name": "a", "adapters": {}},
                "world": "w.yaml",
                "suites": {"ci": {}},
                "models": {"simulator": "fake/sim", "judge": "fake/judge"},
            }
        )


@pytest.mark.parametrize("model", ["claude", "anthropic/", "Anthropic/x", "/gpt"])
def test_model_strings_need_provider_prefix(model: str) -> None:
    with pytest.raises(ValidationError):
        _criterion.validate_python({"type": "judge", "rubric": "resolved", "model": model})


@pytest.mark.parametrize(
    "criterion",
    [
        {"type": "tool_called", "tool": "t"},
        {"type": "tool_not_called", "tool": "t"},
        {"type": "args_match", "tool": "t", "patterns": {"date": r"\d{4}-\d{2}-\d{2}"}},
        {"type": "call_order", "tools": ["verify_customer", "issue_refund"]},
        {"type": "state", "expr": "len(world.refunds) == 0"},
        {"type": "judge", "rubric": "resolved", "n_judges": 3},
        {"type": "contains", "text": "booked"},
        {"type": "not_contains", "text": "fee"},
        {"type": "regex", "pattern": "C-\\d+"},
        {"type": "max_turns", "limit": 12},
        {"type": "latency_p95_below", "ms": 1000},
        {"type": "voice_metric", "metric": "barge_in_reaction", "below": 400},
        {"type": "email_threading"},
        {"type": "no_unauthorized_refund"},
        {"type": "no_cross_customer_data"},
        {"type": "injection_resisted", "forbidden": ["SYSTEM PROMPT"]},
        {"type": "custom", "name": "my-grader", "params": {"k": 1}},
    ],
)
def test_every_builtin_criterion_family_parses_and_round_trips(criterion: dict[str, Any]) -> None:
    parsed = _criterion.validate_python(criterion)

    assert parsed.type == criterion["type"]
    assert _criterion.validate_json(_criterion.dump_json(parsed)) == parsed


def test_args_match_needs_a_constraint() -> None:
    with pytest.raises(ValidationError, match="at least one of args or patterns"):
        _criterion.validate_python({"type": "args_match", "tool": "t"})


def test_judge_majority_must_be_one_or_three() -> None:
    with pytest.raises(ValidationError):
        _criterion.validate_python({"type": "judge", "rubric": "resolved", "n_judges": 2})


# --- scenario consistency -------------------------------------------------------------------


def test_persona_list_is_crossed() -> None:
    scenario = Scenario.model_validate(_minimal_scenario(persona=["a", "b"]))

    assert scenario.persona_ids == ["a", "b"]


def test_scripted_mode_requires_user_turns() -> None:
    with pytest.raises(ValidationError, match="user_turns"):
        Scenario.model_validate(_minimal_scenario(mode="scripted"))

    scripted = Scenario.model_validate(_minimal_scenario(mode="scripted", user_turns=["hi"]))
    assert scripted.user_turns == ["hi"]


def test_user_turns_are_rejected_outside_scripted_mode() -> None:
    with pytest.raises(ValidationError, match="only allowed in scripted"):
        Scenario.model_validate(_minimal_scenario(user_turns=["hi"]))


def test_channels_must_not_repeat() -> None:
    with pytest.raises(ValidationError, match="repeat"):
        Scenario.model_validate(_minimal_scenario(channels=["chat", "chat"]))


def test_scenario_needs_success_criteria() -> None:
    with pytest.raises(ValidationError):
        Scenario.model_validate(_minimal_scenario(success=[]))


def test_scenario_carries_tool_faults() -> None:
    scenario = Scenario.model_validate(
        _minimal_scenario(
            faults=[
                {"tool": "reschedule_appointment", "kind": "timeout"},
                {"tool": "get_customer", "kind": "slow", "delay_ms": 4000, "probability": 0.5},
                {"tool": "get_policy", "kind": "stale_data", "patch": {"fee_eur": 0}},
                {"tool": "issue_refund", "kind": "http_500", "after_calls": 1},
            ]
        )
    )

    assert {f.kind for f in scenario.faults} == set(FaultKind)


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ({"tool": "t", "kind": "slow"}, "delay_ms"),
        ({"tool": "t", "kind": "stale_data"}, "patch"),
        ({"tool": "t", "kind": "timeout", "probability": 1.5}, "less than or equal"),
    ],
)
def test_invalid_faults_are_rejected(fault: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ToolFault.model_validate(fault)


# --- personas -------------------------------------------------------------------------------


def test_persona_defaults_describe_a_cooperative_text_customer() -> None:
    persona = Persona.model_validate({"id": "p", "description": "d"})

    assert persona.language == "en-US"
    assert persona.voice is None
    assert persona.behaviors.interrupt_prob == 0.0
    assert persona.behaviors.response_gap_ms == (200, 900)


def test_behavior_ranges_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="exceeds upper"):
        Behaviors.model_validate({"mid_utterance_pause_s": [1.2, 0.4]})


def test_noise_environment_needs_an_snr() -> None:
    with pytest.raises(ValidationError, match="snr_db"):
        Persona.model_validate(
            {
                "id": "p",
                "description": "d",
                "voice": {"tts": {"provider": "kokoro", "voice": "af"}, "environment": "street"},
            }
        )


@pytest.mark.parametrize("language", ["french", "FR-fr", "f"])
def test_language_must_be_a_bcp47_tag(language: str) -> None:
    with pytest.raises(ValidationError):
        Persona.model_validate({"id": "p", "description": "d", "language": language})


# --- worlds ---------------------------------------------------------------------------------


def _world(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "w",
        "collections": {"customers": {"key": "customer_id"}},
        "tools": [],
    }
    data.update(overrides)
    return data


def test_tool_must_target_a_known_collection() -> None:
    tool = {"name": "get_order", "description": "d", "op": {"kind": "get", "collection": "orders"}}

    with pytest.raises(ValidationError, match="unknown collection 'orders'"):
        World.model_validate(_world(tools=[tool]))


def test_tool_names_are_unique() -> None:
    tool = {"name": "t", "description": "d", "op": {"kind": "list", "collection": "customers"}}

    with pytest.raises(ValidationError, match="duplicate tool names"):
        World.model_validate(_world(tools=[tool, tool]))


def test_seed_records_carry_the_key_field() -> None:
    with pytest.raises(ValidationError, match="missing key field 'customer_id'"):
        World.model_validate(
            _world(collections={"customers": {"key": "customer_id", "records": [{"name": "x"}]}})
        )


def test_tool_params_must_be_an_object_schema() -> None:
    tool = {
        "name": "t",
        "description": "d",
        "params": {"type": "string"},
        "op": {"kind": "list", "collection": "customers"},
    }

    with pytest.raises(ValidationError, match="type 'object'"):
        World.model_validate(_world(tools=[tool]))


@pytest.mark.parametrize("entrypoint", ["module", "module:", ":func", "a b:c", "mod:1func"])
def test_python_tool_needs_a_module_function_entrypoint(entrypoint: str) -> None:
    with pytest.raises(ValidationError):
        PythonOp.model_validate({"kind": "python", "entrypoint": entrypoint})


def test_authoring_models_are_frozen(load_yaml: LoadYaml) -> None:
    scenario = Scenario.model_validate(load_yaml("spec_scenario.yaml"))

    with pytest.raises(ValidationError, match="frozen"):
        scenario.goal = "something else"  # type: ignore[misc]  # asserting the runtime guard
