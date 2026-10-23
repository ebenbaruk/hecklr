"""Run lifecycle step 2: matrix expansion, seeds, cost estimate and budget refusal."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hecklr_core import (
    BudgetEstimateError,
    Suite,
    check_estimate,
    derive_seed,
    estimate_cost,
    expand_matrix,
    extend_matrix,
    plan_run,
    resolve_suite,
)
from hecklr_core.loading import Project
from hecklr_core.planning import (
    PROMPT_CHARS_PER_TOKEN,
    CostEstimate,
    world_snapshot_chars,
)
from hecklr_llm import UnknownPriceError
from hecklr_schemas import (
    Budget,
    Channel,
    ModelPrice,
    RunStatus,
    Scenario,
    ScenarioLimits,
    TrialSpec,
    World,
)

SONNET = "anthropic/claude-sonnet-5-5"


def test_matrix_is_scenario_x_persona_x_channel_x_index(ci_suite: Suite) -> None:
    matrix = expand_matrix(ci_suite, run_seed=7)
    assert [spec.key for spec in matrix] == [
        ("reschedule", "busy", Channel.CHAT, 0),
        ("reschedule", "busy", Channel.CHAT, 1),
        ("reschedule", "calm", Channel.CHAT, 0),
        ("reschedule", "calm", Channel.CHAT, 1),
    ]
    assert len({spec.seed for spec in matrix}) == 4


def test_full_matrix_size(project: Project) -> None:
    suite = resolve_suite(project, "all", trials=3)
    # reschedule: 2 personas x 2 channels; invoice: 1 persona x 1 channel; 3 trials each.
    assert len(expand_matrix(suite, run_seed=0)) == (2 * 2 + 1) * 3


def test_seeds_are_stable_across_runs_and_differ_by_run_seed(ci_suite: Suite) -> None:
    first = expand_matrix(ci_suite, run_seed=7)
    assert first == expand_matrix(ci_suite, run_seed=7)
    other = expand_matrix(ci_suite, run_seed=8)
    assert [s.key for s in other] == [s.key for s in first]
    assert all(a.seed != b.seed for a, b in zip(first, other, strict=True))


def test_derive_seed_known_value() -> None:
    # Pinned (sha256 of '[0, "s", "p", "chat", 0]', top 63 bits) so a refactor cannot silently
    # re-seed every stored baseline.
    assert derive_seed(0, ("s", "p", Channel.CHAT, 0)) == 9059049533202320216


@given(
    seed=st.integers(min_value=-(2**80), max_value=2**80),
    index=st.integers(min_value=0, max_value=10_000),
    scenario=st.text(min_size=1, max_size=20),
)
def test_derived_seeds_are_63_bit_non_negative(seed: int, index: int, scenario: str) -> None:
    value = derive_seed(seed, (scenario, "p", Channel.EMAIL, index))
    assert 0 <= value < 2**63


def test_extend_matrix_appends_new_indices_per_cell(ci_suite: Suite) -> None:
    matrix = expand_matrix(ci_suite, run_seed=7)
    extra = extend_matrix(matrix, 7, ["reschedule", "reschedule"], extra_trials=2)
    assert [spec.key for spec in extra] == [
        ("reschedule", "busy", Channel.CHAT, 2),
        ("reschedule", "busy", Channel.CHAT, 3),
        ("reschedule", "calm", Channel.CHAT, 2),
        ("reschedule", "calm", Channel.CHAT, 3),
    ]
    # Same seeds a fixed design with more trials would have used.
    bigger = expand_matrix(resolve_suite(ci_suite.project, "ci", trials=4), run_seed=7)
    assert set(extra) <= set(bigger)


def test_extend_matrix_rejects_bad_requests(ci_suite: Suite) -> None:
    matrix = expand_matrix(ci_suite, run_seed=7)
    with pytest.raises(ValueError, match="extra_trials"):
        extend_matrix(matrix, 7, ["reschedule"], extra_trials=0)
    with pytest.raises(ValueError, match="not in the run's matrix: \\['ghost'\\]"):
        extend_matrix(matrix, 7, ["ghost"], extra_trials=1)


def _scenario(**fields: object) -> Scenario:
    return Scenario.model_validate(
        {
            "id": "s",
            "title": "t",
            "channels": ["chat"],
            "persona": "p",
            "world": "w",
            "goal": "g",
            "success": [{"type": "judge", "rubric": "resolved"}],
            **fields,
        }
    )


def test_estimate_uses_max_turns_and_prices(ci_suite: Suite) -> None:
    scenario = _scenario(limits=ScenarioLimits(max_turns=4))
    matrix = expand_matrix(ci_suite, run_seed=0)[:1]
    matrix = [matrix[0].model_copy(update={"scenario_id": "s"})]
    estimate = estimate_cost(
        matrix, {"s": scenario}, simulator_model=SONNET, judge_model="openai/gpt-4.1"
    )
    # Simulator: 2 calls, input 2*800 + 80*2*1 = 1760, output 160, at $2/$10 per Mtok.
    assert estimate.simulator_usd == Decimal("0.00512")
    # Judge prompt: 4000 instructions + goal 1 + facts "{}" 2 + 4 turns x 1200 transcript
    # + 4 x 400 world growth = 10403 chars = 5202 tokens. Asked twice (one retry resends the
    # answer): input 2 x 5202 + 1024 = 11428, output 2 x 1024 = 2048, at $2/$8 per Mtok.
    assert estimate.judge_usd == Decimal("0.039240")
    assert estimate.total_usd == Decimal("0.044360")
    assert estimate.trials == 1


def test_estimate_counts_judge_votes_and_overrides(ci_suite: Suite) -> None:
    scenario = _scenario(
        success=[
            {"type": "judge", "rubric": "resolved", "n_judges": 3},
            {"type": "judge", "rubric": "tone", "model": "fake/judge"},
            {"type": "contains", "text": "x"},
        ]
    )
    spec = expand_matrix(ci_suite, run_seed=0)[0].model_copy(update={"scenario_id": "s"})
    single = estimate_cost(
        [spec],
        {"s": _scenario()},
        simulator_model="fake/sim",
        judge_model="openai/gpt-4.1",
    )
    triple = estimate_cost(
        [spec], {"s": scenario}, simulator_model="fake/sim", judge_model="openai/gpt-4.1"
    )
    assert triple.judge_usd == 3 * single.judge_usd
    assert triple.simulator_usd == 0


def test_estimate_is_zero_for_scripted_scenarios_with_deterministic_graders(
    ci_suite: Suite,
) -> None:
    # Issue #51: a scripted customer calls no model, so `--budget-usd 0` must admit the run.
    scripted = _scenario(
        mode="scripted",
        user_turns=["hello", "bye"],
        success=[{"type": "contains", "text": "x"}],
    )
    spec = expand_matrix(ci_suite, run_seed=0)[0].model_copy(update={"scenario_id": "s"})
    estimate = estimate_cost(
        [spec], {"s": scripted}, simulator_model=SONNET, judge_model="openai/gpt-4.1"
    )
    assert estimate.total_usd == 0
    check_estimate(estimate, Budget(usd=Decimal(0)))
    judged = scripted.model_copy(update={"success": _scenario().success})
    estimate = estimate_cost(
        [spec], {"s": judged}, simulator_model=SONNET, judge_model="openai/gpt-4.1"
    )
    assert estimate.simulator_usd == 0
    assert estimate.judge_usd > 0


def test_issue36_a_mixed_suite_charges_the_simulator_per_guided_trial_only(
    ci_suite: Suite,
) -> None:
    # Issue #67 (item 5): scripted and guided scenarios in one matrix.
    free = [{"type": "contains", "text": "x"}]
    scenarios = {
        "scripted": _scenario(mode="scripted", user_turns=["hello"], success=free),
        "guided": _scenario(success=free),
    }
    template = expand_matrix(ci_suite, run_seed=0)[0]
    matrix = [
        template.model_copy(update={"scenario_id": scenario_id, "index": index})
        for scenario_id in ("scripted", "guided", "guided")
        for index in range(2)
    ]

    def estimate(specs: list[TrialSpec]) -> CostEstimate:
        return estimate_cost(specs, scenarios, simulator_model=SONNET, judge_model=SONNET)

    one_guided = estimate(matrix[2:3]).simulator_usd
    assert one_guided > 0
    assert estimate(matrix).simulator_usd == 4 * one_guided
    assert estimate(matrix[:2]).total_usd == 0


def test_estimate_fails_closed_on_unknown_paid_model(ci_suite: Suite) -> None:
    matrix = expand_matrix(ci_suite, run_seed=0)
    with pytest.raises(UnknownPriceError):
        estimate_cost(
            matrix, ci_suite.scenarios, simulator_model="openai/unknown", judge_model="fake/j"
        )
    price = ModelPrice(input_per_mtok=Decimal(0), output_per_mtok=Decimal(0))
    estimate = estimate_cost(
        matrix,
        ci_suite.scenarios,
        simulator_model="openai/unknown",
        judge_model="fake/j",
        prices={"openai/unknown": price},
    )
    assert estimate.total_usd == 0


def test_check_estimate_refuses_over_budget() -> None:
    estimate = CostEstimate(simulator_usd=Decimal("20"), judge_usd=Decimal("6"), trials=600)
    check_estimate(estimate, Budget(usd=None))
    check_estimate(estimate, Budget(usd=Decimal(26)))
    with pytest.raises(BudgetEstimateError) as caught:
        check_estimate(estimate, Budget(usd=Decimal(25)))
    assert str(caught.value) == (
        "estimated cost $26.00 for 600 trials exceeds the budget of $25.00; raise budget.usd "
        "(or --budget-usd), or run fewer trials"
    )
    assert caught.value.estimate_usd == Decimal(26)


def test_plan_run_builds_a_pending_run(ci_suite: Suite) -> None:
    now = datetime(2026, 10, 21, tzinfo=UTC)
    plan = plan_run(ci_suite, agent_version="v2", git_sha="abc", now=now)
    run = plan.run
    assert run.status is RunStatus.PENDING
    assert run.created_at == now
    assert run.suite == "ci"
    assert run.seed == 7
    assert run.config_hash == ci_suite.config_hash
    assert run.matrix == expand_matrix(ci_suite, 7)
    assert run.counts.planned == 4
    assert run.agent.name == "acme-support"
    assert run.agent.version == "v2"
    assert run.agent.adapters == {Channel.CHAT: "http", Channel.EMAIL: "smtp"}
    assert run.budget.usd == Decimal(5)
    assert (run.simulator_model, run.judge_model) == ("fake/sim", "fake/judge")
    assert plan.estimate.total_usd == 0


def test_plan_run_refuses_when_estimate_exceeds_budget(project: Project) -> None:
    config = project.config.model_copy(
        update={
            "models": project.config.models.model_copy(
                update={"simulator": SONNET, "judge": "openai/gpt-4.1"}
            )
        }
    )
    suite = resolve_suite(
        Project(config=config, config_path=project.config_path, world=project.world), "ci"
    )
    with pytest.raises(BudgetEstimateError):
        plan_run(suite, budget_usd=Decimal("0.01"))
    assert plan_run(suite, budget_usd=Decimal(100)).run.budget.usd == Decimal(100)


WORLD = World.model_validate(
    {
        "id": "w",
        "collections": {
            "customers": {
                "key": "id",
                "records": [{"id": f"C-{n}", "address": f"{n} Rue de Rivoli"} for n in range(50)],
            },
            "tickets": {},
        },
    }
)


def test_issue76_world_snapshot_is_sized_as_the_judge_renders_it() -> None:
    chars = world_snapshot_chars(WORLD)
    rendered = {
        "customers": {
            f"C-{n}": {"address": f"{n} Rue de Rivoli", "id": f"C-{n}"} for n in range(50)
        },
        "tickets": {},
    }
    assert chars == len(json.dumps(rendered, sort_keys=True, indent=1))
    assert chars > 50 * len("C-10 Rue de Rivoli")


def test_issue76_world_reading_judges_are_charged_both_snapshots(ci_suite: Suite) -> None:
    spec = expand_matrix(ci_suite, run_seed=0)[0].model_copy(update={"scenario_id": "s"})

    def judge_usd(rubric: str, world: World | None) -> Decimal:
        scenario = _scenario(success=[{"type": "judge", "rubric": rubric}])
        return estimate_cost(
            [spec], {"s": scenario}, simulator_model="fake/s", judge_model=SONNET, world=world
        ).judge_usd

    # Two snapshots per ask, two asks per vote, at $2 per Mtok of input.
    snapshot_tokens = 2 * 2 * world_snapshot_chars(WORLD) / PROMPT_CHARS_PER_TOKEN
    extra = judge_usd("no_hallucination", WORLD) - judge_usd("no_hallucination", None)
    assert abs(extra - Decimal(snapshot_tokens) * 2 / 10**6) < Decimal("0.00001")
    assert judge_usd("tone", WORLD) == judge_usd("tone", None)
    assert judge_usd("handoff_appropriate", WORLD) == judge_usd("tone", WORLD)
    assert judge_usd("tone", WORLD) < judge_usd("resolved", WORLD)


def test_issue76_every_criterion_that_asks_a_model_is_charged(ci_suite: Suite) -> None:
    spec = expand_matrix(ci_suite, run_seed=0)[0].model_copy(update={"scenario_id": "s"})

    def judge_usd(*success: dict[str, object]) -> Decimal:
        scenario = _scenario(mode="scripted", user_turns=["hi"], success=list(success))
        return estimate_cost(
            [spec], {"s": scenario}, simulator_model=SONNET, judge_model=SONNET, world=WORLD
        ).judge_usd

    tone = judge_usd({"type": "judge", "rubric": "tone"})
    injection = judge_usd({"type": "injection_resisted", "forbidden": ["CANARY"]})
    custom = judge_usd({"type": "custom", "name": "no_policy_override"})
    resolved = judge_usd({"type": "judge", "rubric": "resolved"})
    assert injection == tone  # one world-blind vote
    assert custom == resolved  # one vote that may read the world
    assert judge_usd({"type": "no_unauthorized_refund"}, {"type": "contains", "text": "x"}) == 0


def test_issue76_a_world_with_an_unread_seed_file_cannot_be_sized(ci_suite: Suite) -> None:
    world = World.model_validate(
        {"id": "w", "collections": {"customers": {"seed_file": "customers.csv"}}}
    )
    spec = expand_matrix(ci_suite, run_seed=0)[0].model_copy(update={"scenario_id": "s"})
    with pytest.raises(ValueError, match=r"\['customers'\] still name a seed_file"):
        estimate_cost(
            [spec], {"s": _scenario()}, simulator_model=SONNET, judge_model=SONNET, world=world
        )


def test_issue76_plan_run_sizes_judges_with_the_project_world(ci_suite: Suite) -> None:
    project = ci_suite.project
    models = project.config.models.model_copy(update={"judge": SONNET})
    paid = replace(project, config=project.config.model_copy(update={"models": models}))
    suite = replace(ci_suite, project=paid)
    plan = plan_run(suite)

    def estimate(world: World | None) -> CostEstimate:
        return estimate_cost(
            plan.run.matrix,
            suite.scenarios,
            simulator_model=models.simulator,
            judge_model=SONNET,
            world=world,
        )

    assert plan.estimate == estimate(project.world)
    assert plan.estimate.judge_usd > estimate(None).judge_usd
