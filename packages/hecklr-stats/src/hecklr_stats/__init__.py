"""Hecklr statistics: intervals, paired tests, verdicts, adaptive design.

Inputs are plain ``TrialOutcome`` records; outputs are ``hecklr_schemas`` ``Estimate``,
``MetricSummary`` and ``Comparison`` objects. Every resampling function takes an explicit seed.

Typical use by the orchestrator and CLI::

    summaries = summarize_run(outcomes, run_id=run.id, seed=run.seed, resamples=10_000)
    comparisons = compare_runs(
        baseline_outcomes, candidate_outcomes,
        baseline_run=baseline.id, candidate_run=run.id, config=config.stats, seed=run.seed,
    )
"""

from hecklr_stats.compare import (
    PASS_RATE,
    PairedResult,
    benjamini_hochberg,
    compare_runs,
    compare_sequentially,
    decide_verdict,
    pair,
    paired_comparison,
    sign_flip_p_value,
    trials_needed,
)
from hecklr_stats.flaky import FlakyScenario, flaky_scenarios
from hecklr_stats.intervals import quantile_estimates, wilson_estimate, wilson_interval
from hecklr_stats.judge import (
    KAPPA_TRUST_THRESHOLD,
    JudgeCalibration,
    JudgeDrift,
    calibrate_judge,
    cohen_kappa,
    judge_drift,
    kappa_estimate,
)
from hecklr_stats.outcomes import ScenarioCounts, TrialOutcome
from hecklr_stats.rates import (
    pass_at_k,
    pass_hat_k,
    scenario_pass_at_k,
    scenario_pass_hat_k,
    scenario_pass_rate,
    suite_pass_at_k,
    suite_pass_hat_k,
    suite_pass_rate,
)
from hecklr_stats.sequential import (
    Boundary,
    allocate_trials,
    group_sequential_boundaries,
    information_fraction,
    obrien_fleming_spending,
)
from hecklr_stats.summary import summarize_run
from hecklr_stats.validation import REALISTIC_SUITE, SuiteModel, verdict_rate

__version__ = "0.1.0"

__all__ = [
    "KAPPA_TRUST_THRESHOLD",
    "PASS_RATE",
    "REALISTIC_SUITE",
    "Boundary",
    "FlakyScenario",
    "JudgeCalibration",
    "JudgeDrift",
    "PairedResult",
    "ScenarioCounts",
    "SuiteModel",
    "TrialOutcome",
    "__version__",
    "allocate_trials",
    "benjamini_hochberg",
    "calibrate_judge",
    "cohen_kappa",
    "compare_runs",
    "compare_sequentially",
    "decide_verdict",
    "flaky_scenarios",
    "group_sequential_boundaries",
    "information_fraction",
    "judge_drift",
    "kappa_estimate",
    "obrien_fleming_spending",
    "pair",
    "paired_comparison",
    "pass_at_k",
    "pass_hat_k",
    "quantile_estimates",
    "scenario_pass_at_k",
    "scenario_pass_hat_k",
    "scenario_pass_rate",
    "sign_flip_p_value",
    "suite_pass_at_k",
    "suite_pass_hat_k",
    "suite_pass_rate",
    "summarize_run",
    "trials_needed",
    "verdict_rate",
    "wilson_estimate",
    "wilson_interval",
]
