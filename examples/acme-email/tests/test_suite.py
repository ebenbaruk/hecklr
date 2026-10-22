"""The email suites load, and the scripted ones are an exact F11/F12 fault matrix (SC8).

Each scripted scenario runs as a real graded email trial in process (scripted customer, in-process
email adapter, ``EmailDriver``, ``TrialWorld`` with the scenario's injected faults, graders).
"""

import pytest
from acme_email import EMAIL_ROOT
from email_testing import email_desk, load_scenarios, run_email_trial

from hecklr_core import load_project, resolve_suite
from hecklr_grade import build_graders
from hecklr_schemas import Channel, Persona, Scenario, World

SCRIPTED = load_scenarios("scripted/*.yaml")
GUIDED = load_scenarios("scenarios/**/*.yaml")
CONFIGS = ("hecklr.yaml", "hecklr.mailbox.yaml")


@pytest.mark.parametrize("config", CONFIGS)
@pytest.mark.parametrize("suite", ["ci", "full", "smoke"])
def test_every_suite_resolves_to_email_trials(config: str, suite: str) -> None:
    project = load_project(EMAIL_ROOT / config)
    resolved = resolve_suite(project, suite)
    assert resolved.cells
    assert {channel for cell in resolved.cells for channel in cell.channels} == {Channel.EMAIL}


def test_every_criterion_builds_a_grader() -> None:
    for scenario in SCRIPTED + GUIDED:
        assert build_graders(scenario)


@pytest.mark.parametrize("fault", ["f11", "f12"])
def test_sc8_each_fault_has_at_least_six_scenarios_in_each_set(fault: str) -> None:
    # A fully flipped fault then has a paired sign-flip p of at most 2 / 2^6 < 0.05 (ADR 0008a).
    for scenarios in (SCRIPTED, [s for s in GUIDED if "core" in s.tags]):
        assert len([s for s in scenarios if f"catches-{fault}" in s.tags]) >= 6


def test_every_email_scenario_grades_threading() -> None:
    for scenario in SCRIPTED + GUIDED:
        assert "catches-f11" in scenario.tags
        assert any(criterion.type == "email_threading" for criterion in scenario.success)


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", SCRIPTED, ids=lambda s: s.id)
async def test_sc8_clean_email_agent_passes_every_scripted_scenario(
    scenario: Scenario, personas: dict[str, Persona], world: World
) -> None:
    trial = await run_email_trial(scenario, personas[scenario.persona_ids[0]], world, email_desk())
    assert trial.passed, trial.failed()


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", SCRIPTED, ids=lambda s: s.id)
async def test_sc8_f11_fails_threading_on_every_scripted_scenario(
    scenario: Scenario, personas: dict[str, Persona], world: World
) -> None:
    trial = await run_email_trial(
        scenario, personas[scenario.persona_ids[0]], world, email_desk("F11")
    )
    assert trial.failed() == ["email_threading"]
    for reply in trial.emails("agent"):
        assert reply.message.in_reply_to is None
        assert reply.message.references == []


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", SCRIPTED, ids=lambda s: s.id)
async def test_sc8_f12_fails_exactly_the_tool_timeout_scenarios(
    scenario: Scenario, personas: dict[str, Persona], world: World
) -> None:
    trial = await run_email_trial(
        scenario, personas[scenario.persona_ids[0]], world, email_desk("F12")
    )
    if "catches-f12" in scenario.tags:
        assert not trial.passed
        assert trial.trial.status == "timeout"  # the customer's email was never answered
    else:
        assert trial.passed, trial.failed()
