"""The private brief and turn prompt: persona traits as text, facts discipline, channel wording."""

from collections.abc import Callable

import pytest

from hecklr_schemas import Channel, DialogueTurn, Persona, Scenario, TrialBrief
from hecklr_sim.brief import render_brief, render_fact_value, render_turn_prompt

BriefFactory = Callable[..., TrialBrief]


def _brief_text(brief: TrialBrief) -> str:
    return render_brief(brief.scenario, brief.persona, brief.channel, None)


@pytest.mark.parametrize(
    ("traits", "expected"),
    [
        ({"verbosity": 0.0}, "very short"),
        ({"verbosity": 0.5}, "one or two sentences"),
        ({"verbosity": 0.9}, "chatty"),
        ({"patience": 0.1}, "impatient"),
        ({"patience": 0.9}, "very patient"),
        ({"cooperativeness": 0.1}, "guarded"),
        ({"cooperativeness": 0.5}, "without offering much"),
        ({"cooperativeness": 0.9}, "may volunteer"),
    ],
)
def test_ws2_traits_become_behavior_guidance(
    make_brief: BriefFactory, traits: dict[str, float], expected: str
) -> None:
    assert expected in _brief_text(make_brief(persona={"traits": traits}))


def test_ws2_fillers_behavior_is_textual_guidance(make_brief: BriefFactory) -> None:
    assert "fillers" in _brief_text(make_brief(persona={"behaviors": {"fillers": True}}))
    assert "fillers" not in _brief_text(make_brief())


def test_ws2_brief_without_facts_tells_the_customer_not_to_invent(make_brief: BriefFactory) -> None:
    text = _brief_text(make_brief(scenario={"facts": {}}))
    assert "No specific details" in text


@pytest.mark.parametrize(
    ("channel", "expected"),
    [
        (Channel.CHAT, "support chat window"),
        (Channel.VOICE, "phone call"),
        (Channel.EMAIL, "subject line for your first email"),
    ],
)
def test_ws2_brief_matches_the_channel(
    make_brief: BriefFactory, channel: Channel, expected: str
) -> None:
    assert expected in _brief_text(make_brief(channel=channel))


def test_ws2_brief_never_reveals_it_is_a_simulation_to_the_agent(make_brief: BriefFactory) -> None:
    assert "never mention being an AI, a simulation or a test" in _brief_text(make_brief())


def test_ws2_fact_values_render_as_the_customer_would_say_them() -> None:
    assert render_fact_value("C-1042") == "C-1042"
    assert render_fact_value(42) == "42"
    assert render_fact_value(["a", "é"]) == '["a", "é"]'


def test_ws2_turn_prompt_shows_state_and_feedback() -> None:
    dialogue = [DialogueTurn(speaker="agent", text="Hello")]

    calm = render_turn_prompt(dialogue, patience_left=4, turns_left=9, first_email=False)
    rushed = render_turn_prompt(
        dialogue, patience_left=1, turns_left=1, first_email=True, feedback=["wrong ID"]
    )

    assert "AGENT: Hello" in calm
    assert "4 more agent messages" in calm
    assert "9 more message(s)" in calm
    assert "rejected" not in calm
    assert "about to give up" in rushed
    assert "subject line" in rushed
    assert "- wrong ID" in rushed


def test_ws2_opening_prompt_asks_for_an_opening_message() -> None:
    prompt = render_turn_prompt([], patience_left=3, turns_left=24, first_email=False)
    assert "Write your opening message" in prompt


def test_ws2_brief_uses_scenario_and_persona_only() -> None:
    scenario = Scenario.model_validate(
        {
            "id": "s",
            "title": "t",
            "channels": ["chat"],
            "persona": "p",
            "world": "w",
            "goal": "Cancel my contract",
            "success": [{"type": "judge", "rubric": "resolved"}],
        }
    )
    text = render_brief(scenario, Persona(id="p", description="A retiree"), Channel.CHAT, None)
    assert "Cancel my contract" in text
    assert "A retiree" in text
