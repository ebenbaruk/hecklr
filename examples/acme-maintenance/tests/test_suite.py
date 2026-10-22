"""The acme-maintenance suite: valid for hecklr-core, sized per ADR 0005a, and catching F1-F6.

The scripted smoke suite runs every scripted scenario as a real graded trial (in process, rules
brain): the clean agent passes all of them, and each fault variant fails exactly the scenarios
tagged ``catches-<fault>``. That is the SC8 fault matrix on deterministic graders, for free.
"""

from collections import Counter

import pytest
from acme_agent import PROJECT_ROOT
from acme_testing import FAULTS, TrialOutcome, load_scenarios, rules_agent, run_scripted_trial

from hecklr_core import load_project, resolve_suite
from hecklr_grade import build_graders
from hecklr_schemas import Persona, Scenario, SimulationMode, World

GUIDED = load_scenarios("scenarios/**/*.yaml")
SCRIPTED = load_scenarios("scripted/*.yaml")
REDTEAM = load_scenarios("redteam/*.yaml")


def test_every_suite_resolves_with_hecklr_core() -> None:
    project = load_project(PROJECT_ROOT / "hecklr.yaml")
    sizes = {name: len(resolve_suite(project, name).cells) for name in project.config.suites}
    assert sizes == {"ci": 40, "full": 64, "redteam": 30, "smoke": 33}
    assert project.world.id == "acme-maintenance"
    assert project.config.agent.editable == ["prompts/system.md", "tools/descriptions.yaml"]


def test_reference_suite_meets_adr_0005a_size() -> None:
    project = load_project(PROJECT_ROOT / "hecklr.yaml")
    full = resolve_suite(project, "full")
    assert len(full.cells) >= 60
    assert full.trials == 5


def test_scenarios_cover_every_area_and_persona(personas: dict[str, Persona]) -> None:
    areas = Counter(path.parent.name for path in PROJECT_ROOT.glob("scenarios/*/*.yaml"))
    assert areas == {
        "scheduling": 23,
        "billing": 9,
        "refunds": 13,
        "emergency": 11,
        "data-protection": 8,
    }
    used = {persona for scenario in GUIDED + REDTEAM for persona in scenario.persona_ids}
    assert used == set(personas)
    assert len(personas) >= 8
    assert {"busy-homeowner", "refund-fraudster", "data-prober"} <= used


@pytest.mark.parametrize(
    ("tag", "minimum"),
    [
        ("reschedule", 6),
        ("booking", 6),
        ("cancellation", 6),
        ("refunds", 6),
        ("emergency", 6),
        ("data-protection", 6),
        ("redteam", 6),
    ],
)
def test_each_fault_area_has_enough_scenarios(tag: str, minimum: int) -> None:
    tagged = [s for s in GUIDED if tag in s.tags]
    core = [s for s in tagged if "core" in s.tags]
    assert len(tagged) >= minimum
    assert len(core) >= min(minimum, 3)


@pytest.mark.parametrize(
    ("fault", "grader_types"),
    [
        ("F1", {"tool_called", "judge:resolved"}),
        ("F2", {"state"}),
        ("F3", {"judge:no_hallucination"}),
        ("F4", {"no_cross_customer_data"}),
        ("F5", {"no_unauthorized_refund"}),
        ("F6", {"judge:handoff_appropriate"}),
    ],
)
def test_sc8_the_spec_grader_for_each_fault_is_in_the_suite(
    fault: str, grader_types: set[str]
) -> None:
    area = {
        "F1": "reschedule",
        "F2": "reschedule",
        "F3": "cancellation",
        "F4": "data-protection",
        "F5": "refunds",
        "F6": "emergency",
    }[fault]
    found = {
        f"judge:{c.rubric}" if c.type == "judge" else c.type
        for scenario in GUIDED
        if area in scenario.tags
        for c in scenario.success
    }
    assert grader_types <= found


def test_data_probes_name_the_caller_and_use_the_adversarial_persona() -> None:
    probes = [s for s in GUIDED if "data-protection" in s.tags]
    for scenario in probes:
        assert any(c.type == "no_cross_customer_data" for c in scenario.success)
        if "redteam" in scenario.tags:
            assert scenario.persona == "data-prober"
            assert scenario.mode is SimulationMode.ADVERSARIAL
        # A caller who is a customer names their own record; anyone else owns none (ADR 0004a).
        assert scenario.facts.get("customer_id") != "C-2077"


@pytest.mark.parametrize("scenario", GUIDED + SCRIPTED, ids=lambda s: s.id)
def test_every_criterion_builds_a_grader(scenario: Scenario) -> None:
    graders = build_graders(scenario)
    assert len([g for g in graders if g.criterion_index is not None]) == len(scenario.success)


def test_world_facts_match_the_world(world: World) -> None:
    customers = {c["customer_id"]: c for c in world.collections["customers"].records}
    for scenario in GUIDED + SCRIPTED:
        customer = scenario.facts.get("customer_id")
        if customer is not None and "postcode" in scenario.facts:
            assert customers[str(customer)]["postcode"] == scenario.facts["postcode"], scenario.id


# --- the deterministic fault matrix -----------------------------------------------------------


async def _matrix_row(
    variant: str, world: World, personas: dict[str, Persona]
) -> dict[str, TrialOutcome]:
    return {
        scenario.id: await run_scripted_trial(
            scenario, personas[scenario.persona_ids[0]], world, rules_agent(variant)
        )
        for scenario in SCRIPTED
    }


@pytest.mark.anyio
async def test_sc8_clean_agent_passes_every_scripted_scenario(
    world: World, personas: dict[str, Persona]
) -> None:
    outcomes = await _matrix_row("clean", world, personas)
    failing = {sid: (o.failed(), o.transcript()) for sid, o in outcomes.items() if not o.passed}
    assert failing == {}
    assert all(outcome.trial.status == "ok" for outcome in outcomes.values())


@pytest.mark.anyio
@pytest.mark.parametrize("fault", FAULTS)
async def test_sc8_each_fault_fails_exactly_its_scenarios(
    fault: str, world: World, personas: dict[str, Persona]
) -> None:
    outcomes = await _matrix_row(fault, world, personas)
    failing = {sid for sid, outcome in outcomes.items() if not outcome.passed}
    expected = {s.id for s in SCRIPTED if f"catches-{fault.lower()}" in s.tags}
    assert failing == expected
    # Six fully flipped scenarios give a paired sign-flip p of 2 / 2**6 < 0.05 (ADR 0008a).
    assert len(expected) >= 6
