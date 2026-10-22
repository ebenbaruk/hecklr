"""Run lifecycle step 2, Plan: expand the trial matrix, derive seeds, estimate cost, refuse early.

Seeds are derived from the run seed and the cell's ``TrialSpec.key`` with SHA-256, so the same
cell gets the same seed in every run with the same root seed: baseline and candidate trials pair
up and replay the same simulated customer.
"""

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from hecklr_core.errors import BudgetEstimateError
from hecklr_core.loading import Suite
from hecklr_llm import PRICES, LLMUsage, cost_usd
from hecklr_schemas import (
    AgentRef,
    Budget,
    Channel,
    JudgeCriterion,
    ModelPrice,
    Run,
    RunCounts,
    RunId,
    RunStatus,
    Scenario,
    SimulationMode,
    TrialSpec,
    new_id,
)

# Token heuristics for the pre-run estimate. They describe a conversation that runs all the way
# to ``limits.max_turns``, so the estimate is an upper bound for typical trials, and they are
# deliberately generous: refusing a run that would have fit is cheaper than overspending.
SIM_BRIEF_TOKENS: Final = 800
"""Simulator system prompt: persona, goal, facts and rules."""
TOKENS_PER_TURN: Final = 80
"""Average size of one message of the dialogue, either party."""
SIM_OUTPUT_TOKENS: Final = 80
"""One simulated customer message."""
JUDGE_PROMPT_TOKENS: Final = 1500
"""Rubric, scenario, world snapshot and tool results sent to a judge, besides the transcript."""
JUDGE_OUTPUT_TOKENS: Final = 300
"""Verdict, rationale and evidence ids."""

_SEED_BITS: Final = 63


def derive_seed(run_seed: int, key: tuple[str, str, Channel, int]) -> int:
    """Seed of one matrix cell: SHA-256 of the run seed and the cell key, as a 63-bit integer."""
    scenario_id, persona_id, channel, index = key
    material = json.dumps([run_seed, scenario_id, persona_id, channel.value, index])
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") >> (64 - _SEED_BITS)


def make_spec(
    run_seed: int, scenario_id: str, persona_id: str, channel: Channel, index: int
) -> TrialSpec:
    """One cell with its derived seed."""
    return TrialSpec(
        scenario_id=scenario_id,
        persona_id=persona_id,
        channel=channel,
        index=index,
        seed=derive_seed(run_seed, (scenario_id, persona_id, channel, index)),
    )


def expand_matrix(suite: Suite, run_seed: int) -> list[TrialSpec]:
    """Scenario x persona x channel x trial index, in a stable order."""
    return [
        make_spec(run_seed, cell.scenario.id, persona_id, channel, index)
        for cell in suite.cells
        for persona_id in cell.persona_ids
        for channel in cell.channels
        for index in range(suite.trials)
    ]


def extend_matrix(
    matrix: Sequence[TrialSpec], run_seed: int, scenario_ids: Iterable[str], extra_trials: int
) -> list[TrialSpec]:
    """New cells for adaptive trials, numbered after each cell's highest existing index.

    Every persona and channel the given scenarios already run with gets ``extra_trials`` more
    indices.

    Raises:
        ValueError: ``extra_trials`` is not positive or a scenario is not in the matrix.
    """
    if extra_trials < 1:
        raise ValueError(f"extra_trials must be >= 1, got {extra_trials}")
    next_index: dict[tuple[str, str, Channel], int] = {}
    for spec in matrix:
        cell = (spec.scenario_id, spec.persona_id, spec.channel)
        next_index[cell] = max(next_index.get(cell, 0), spec.index + 1)
    wanted = list(dict.fromkeys(scenario_ids))
    missing = sorted(set(wanted) - {scenario for scenario, _, _ in next_index})
    if missing:
        raise ValueError(f"scenarios not in the run's matrix: {missing}")
    return [
        make_spec(run_seed, scenario, persona, channel, start + offset)
        for (scenario, persona, channel), start in next_index.items()
        if scenario in wanted
        for offset in range(extra_trials)
    ]


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """Upper-bound model spend of a planned run, by role."""

    simulator_usd: Decimal
    judge_usd: Decimal
    trials: int

    @property
    def total_usd(self) -> Decimal:
        """Simulator plus judges."""
        return self.simulator_usd + self.judge_usd


def estimate_cost(
    matrix: Sequence[TrialSpec],
    scenarios: Mapping[str, Scenario],
    *,
    simulator_model: str,
    judge_model: str,
    prices: Mapping[str, ModelPrice] | None = None,
) -> CostEstimate:
    """Estimate spend from each scenario's ``limits.max_turns`` (see the token heuristics above).

    The simulator speaks every other turn and sees the whole dialogue so far each time; a
    scripted customer reads its fixed ``user_turns`` and calls no model. Every judge criterion
    reads the full transcript once per judge vote; deterministic criteria are free.

    Raises:
        UnknownPriceError: a paid model has no known price.
    """
    table = {**PRICES, **(prices or {})}
    simulator = Decimal(0)
    judge = Decimal(0)
    for spec in matrix:
        scenario = scenarios[spec.scenario_id]
        max_turns = scenario.limits.max_turns
        if scenario.mode is not SimulationMode.SCRIPTED:
            simulator += cost_usd(simulator_model, _simulator_usage(max_turns), table)
        transcript_tokens = max_turns * TOKENS_PER_TURN
        for criterion in scenario.success:
            if isinstance(criterion, JudgeCriterion):
                usage = LLMUsage(
                    input_tokens=JUDGE_PROMPT_TOKENS + transcript_tokens,
                    output_tokens=JUDGE_OUTPUT_TOKENS,
                )
                model = criterion.model or judge_model
                judge += criterion.n_judges * cost_usd(model, usage, table)
    return CostEstimate(simulator_usd=simulator, judge_usd=judge, trials=len(matrix))


def _simulator_usage(max_turns: int) -> LLMUsage:
    calls = math.ceil(max_turns / 2)
    # Call k (0-based) sees the brief plus the 2k messages exchanged before it.
    dialogue = TOKENS_PER_TURN * 2 * (calls * (calls - 1) // 2)
    return LLMUsage(
        input_tokens=calls * SIM_BRIEF_TOKENS + dialogue,
        output_tokens=calls * SIM_OUTPUT_TOKENS,
    )


def check_estimate(estimate: CostEstimate, budget: Budget) -> None:
    """Refuse a run whose estimate exceeds ``budget.usd``.

    Raises:
        BudgetEstimateError: the estimate is over budget.
    """
    if budget.usd is not None and estimate.total_usd > budget.usd:
        raise BudgetEstimateError(estimate.total_usd, budget.usd, estimate.trials)


@dataclass(frozen=True, slots=True)
class RunPlan:
    """A planned run, not yet started, and what it is expected to cost."""

    run: Run
    suite: Suite
    estimate: CostEstimate


def plan_run(
    suite: Suite,
    *,
    budget_usd: Decimal | None = None,
    agent_version: str | None = None,
    git_sha: str | None = None,
    baseline_id: RunId | None = None,
    now: datetime | None = None,
) -> RunPlan:
    """Expand ``suite`` into a pending ``Run`` and check its estimate against the budget.

    ``budget_usd`` overrides ``budget.usd`` from ``hecklr.yaml`` (``--budget-usd``).

    Raises:
        BudgetEstimateError: the estimate exceeds the budget.
        UnknownPriceError: a paid model has no known price.
    """
    config = suite.project.config
    budget = config.budget
    if budget_usd is not None:
        budget = budget.model_copy(update={"usd": budget_usd})
    matrix = expand_matrix(suite, config.seed)
    estimate = estimate_cost(
        matrix,
        suite.scenarios,
        simulator_model=config.models.simulator,
        judge_model=config.models.judge,
        prices=config.models.prices,
    )
    check_estimate(estimate, budget)
    adapters = config.agent.adapters
    run = Run(
        id=RunId(new_id()),
        created_at=now or datetime.now(UTC),
        git_sha=git_sha,
        agent=AgentRef(
            name=config.agent.name,
            version=agent_version,
            adapters={
                channel: getattr(adapters, channel.value).type for channel in adapters.channels()
            },
        ),
        config_hash=suite.config_hash,
        seed=config.seed,
        suite=suite.name,
        baseline_id=baseline_id,
        adaptive=False,
        matrix=matrix,
        budget=budget,
        status=RunStatus.PENDING,
        counts=RunCounts(planned=len(matrix)),
        simulator_model=config.models.simulator,
        judge_model=config.models.judge,
    )
    return RunPlan(run=run, suite=suite, estimate=estimate)
