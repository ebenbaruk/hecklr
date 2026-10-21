"""Replay extraction (US-02): 20 fixture transcripts become adaptive replay scenarios."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from hecklr_llm import FakeLLM, LLMOutputError
from hecklr_schemas import (
    Channel,
    Conversation,
    JudgeCriterion,
    LLMRequest,
    Persona,
    Scenario,
    SimTurn,
    SimulationMode,
    ToolCalledCriterion,
    TrialBrief,
    TrialId,
    new_id,
)
from hecklr_sim import (
    ExtractedFact,
    GuidedSimulator,
    derive_seed,
    extract_scenario,
    grounded_facts,
    load_conversations,
    make_simulator,
    scenario_id_for,
    scenario_to_yaml,
    transcripts_to_scenarios,
)
from hecklr_sim.testing import decision_reply

FIXTURES = Path(__file__).parent / "fixtures"
TRANSCRIPTS = FIXTURES / "transcripts.jsonl"
EXPECTED: dict[str, dict[str, Any]] = json.loads(
    (FIXTURES / "replay_extractions.json").read_text(encoding="utf-8")
)
_CONVERSATION_ID = re.compile(r"^Conversation (\S+) \(")


def _extractor(request: LLMRequest) -> dict[str, Any]:
    """FakeLLM responder: the canned extraction for the conversation in the prompt."""
    match = _CONVERSATION_ID.match(request.messages[-1].content)
    assert match is not None
    reply: dict[str, Any] = EXPECTED[match.group(1)]["reply"]
    return reply


def _conversation(**fields: Any) -> Conversation:
    return Conversation.model_validate(
        {"id": "c-1", "turns": [{"role": "user", "text": "My ID is C-1"}], **fields}
    )


def _listing(directory: Path) -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in directory.iterdir()}


def test_us02_fixture_set_has_20_varied_transcripts() -> None:
    conversations = load_conversations(TRANSCRIPTS)

    assert len(conversations) == 20
    assert {c.id for c in conversations} == set(EXPECTED)
    assert {c.channel for c in conversations} == {Channel.CHAT, Channel.VOICE, Channel.EMAIL}


@pytest.mark.anyio
async def test_us02_replay_extraction_on_20_transcripts() -> None:
    llm = FakeLLM(responder=_extractor)

    for conversation in load_conversations(TRANSCRIPTS):
        scenario = await extract_scenario(
            conversation, llm, model="fake/extractor", world="acme-maintenance", persona="caller"
        )
        expected = EXPECTED[conversation.id]
        reply = expected["reply"]

        assert scenario.mode is SimulationMode.REPLAY
        assert scenario.id == scenario_id_for(conversation.id)
        assert scenario.source is not None
        assert (scenario.source.kind, scenario.source.conversation_id) == (
            "replay",
            conversation.id,
        )
        assert scenario.channels == [conversation.channel]
        assert (scenario.world, scenario.persona) == ("acme-maintenance", "caller")
        assert scenario.goal == reply["goal"]
        assert scenario.title == reply["title"]
        assert scenario.facts == expected["expected_facts"], conversation.id
        assert scenario.tags[0] == "replay"
        assert set(reply["tags"]) <= set(scenario.tags)
        assert scenario.limits.max_turns >= 2 * len(conversation.turns)
        tools = [c.tool for c in scenario.success if isinstance(c, ToolCalledCriterion)]
        assert tools == ([expected["expected_tool"]] if expected["expected_tool"] else [])
        assert JudgeCriterion(type="judge", rubric="resolved") in scenario.success
        assert Scenario.model_validate(yaml.safe_load(scenario_to_yaml(scenario))) == scenario

    assert len(llm.calls) == 20


@pytest.mark.anyio
async def test_us02_extraction_request_is_seeded_by_conversation_and_carries_tool_calls() -> None:
    conversation = load_conversations(TRANSCRIPTS)[0]
    llm = FakeLLM(responder=_extractor)

    for _ in range(2):
        await extract_scenario(conversation, llm, model="fake/x", world="w", persona="p", seed=3)

    first, second = llm.calls
    assert first == second
    assert first.seed == derive_seed(3, "replay", conversation.id)
    assert first.schema_name == "ReplayExtraction"
    prompt = first.messages[-1].content
    assert "CUSTOMER: Hi, I need to move my boiler service on Thursday" in prompt
    assert "[agent called tool reschedule_appointment(" in prompt


def test_us02_grounding_drops_values_the_customer_never_said() -> None:
    conversation = Conversation.model_validate(
        {
            "id": "c",
            "turns": [
                {"role": "user", "text": "Account c-2675, postcode 92000"},
                {"role": "agent", "text": "Ticket T-901 opened."},
            ],
        }
    )
    facts = [
        ExtractedFact(name="Customer ID", value="C-2675"),
        ExtractedFact(name="customer-id", value="C-0000"),
        ExtractedFact(name="ticket", value="T-901"),
        ExtractedFact(name="!!!", value="92000"),
        ExtractedFact(name="postcode", value="  "),
        ExtractedFact(name="postcode", value="92000"),
    ]

    assert grounded_facts(facts, conversation) == {"customer_id": "C-2675", "postcode": "92000"}


@pytest.mark.anyio
async def test_us02_transcripts_to_scenarios_writes_one_yaml_per_conversation(
    tmp_path: Path,
) -> None:
    out = tmp_path / "scenarios" / "replay"

    paths = await transcripts_to_scenarios(
        TRANSCRIPTS, out, FakeLLM(responder=_extractor), model="fake/x", world="w", persona="p"
    )

    assert [p.name for p in paths] == [
        f"{scenario_id_for(c.id)}.yaml" for c in load_conversations(TRANSCRIPTS)
    ]
    for path in paths:
        scenario = Scenario.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        assert scenario.mode is SimulationMode.REPLAY
    assert "schema_version" not in paths[0].read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_us02_transcripts_to_scenarios_never_overwrites(tmp_path: Path) -> None:
    (tmp_path / "replay-prod-88230.yaml").write_text("hand edited\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match=r"replay-prod-88230\.yaml"):
        await transcripts_to_scenarios(
            TRANSCRIPTS,
            tmp_path,
            FakeLLM(responder=_extractor),
            model="fake/x",
            world="w",
            persona="p",
        )

    assert _listing(tmp_path) == {"replay-prod-88230.yaml": "hand edited\n"}


@pytest.mark.anyio
async def test_us02_transcripts_to_scenarios_rejects_colliding_ids(tmp_path: Path) -> None:
    source = tmp_path / "in.jsonl"
    lines = [_conversation(id=cid).model_dump_json() for cid in ("Prod 1", "prod-1")]
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="both map to scenario id 'replay-prod-1'"):
        await transcripts_to_scenarios(
            source, tmp_path / "out", FakeLLM(), model="fake/x", world="w", persona="p"
        )


def test_us02_load_conversations_skips_blank_lines_and_names_bad_ones(tmp_path: Path) -> None:
    source = tmp_path / "in.jsonl"
    source.write_text(f'\n{_conversation().model_dump_json()}\n\n{{"id": "x"}}\n', encoding="utf-8")

    with pytest.raises(ValueError, match=r"in\.jsonl:4: not a valid conversation"):
        load_conversations(source)

    source.write_text(f"{_conversation().model_dump_json()}\n\n", encoding="utf-8")
    assert [c.id for c in load_conversations(source)] == ["c-1"]


@pytest.mark.parametrize(
    ("conversation_id", "scenario_id"),
    [
        ("prod-88213", "replay-prod-88213"),
        ("Zendesk #42 / FR", "replay-zendesk-42-fr"),
        ("###", "replay-conversation"),
    ],
)
def test_us02_scenario_ids_are_valid_slugs(conversation_id: str, scenario_id: str) -> None:
    assert scenario_id_for(conversation_id) == scenario_id
    assert len(scenario_id_for("x" * 300)) == 128


@pytest.mark.anyio
async def test_us02_extraction_falls_back_to_a_title_and_rejects_an_empty_goal() -> None:
    base: dict[str, Any] = {
        "facts": [],
        "resolving_tool": "issue_refund",
        "tags": ["  ", "Billing!"],
    }
    untitled = FakeLLM(script=[{**base, "title": " ", "goal": "Get a refund"}])
    goalless = FakeLLM(script=[{**base, "title": "t", "goal": "  "}])

    scenario = await extract_scenario(
        _conversation(), untitled, model="fake/x", world="w", persona="p"
    )

    assert scenario.title == "Replay of c-1"
    assert scenario.tags == ["replay", "billing"]
    assert scenario.success == [JudgeCriterion(type="judge", rubric="resolved")]
    with pytest.raises(ValueError, match="no goal"):
        await extract_scenario(_conversation(), goalless, model="fake/x", world="w", persona="p")


@pytest.mark.anyio
async def test_us02_malformed_extraction_fails_loudly() -> None:
    with pytest.raises(LLMOutputError):
        await extract_scenario(
            _conversation(), FakeLLM(script=["{}"]), model="fake/x", world="w", persona="p"
        )


@pytest.mark.anyio
async def test_us02_replay_scenario_runs_as_an_adaptive_simulation() -> None:
    conversation = load_conversations(TRANSCRIPTS)[0]
    scenario = await extract_scenario(
        conversation, FakeLLM(responder=_extractor), model="fake/x", world="w", persona="p"
    )
    brief = TrialBrief(
        trial_id=TrialId(new_id()),
        scenario=scenario,
        persona=Persona(id="p", description="A homeowner"),
        channel=Channel.CHAT,
        seed=1,
    )
    llm = FakeLLM(script=[decision_reply("Hi, I need to move my boiler service.")])

    simulator = make_simulator("fake/sim")(brief, llm)

    assert isinstance(simulator, GuidedSimulator)
    assert await simulator.next_turn([]) == SimTurn(text="Hi, I need to move my boiler service.")
    system = llm.calls[0].system or ""
    assert scenario.goal in system
    assert "customer_id: C-1042" in system
