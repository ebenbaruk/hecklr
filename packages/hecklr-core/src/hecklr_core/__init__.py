"""Hecklr orchestrator: loading, matrix expansion, budgets, trial execution and storage.

Typical wiring (done by the ``hecklr`` CLI)::

    project = load_project(Path("hecklr.yaml"))
    plan = plan_run(resolve_suite(project, "ci"), git_sha=sha)
    async with (
        open_llm(project.config, project.output_dir) as llm,
        ResultStore(project.output_dir / RUNS_DIR) as store,
    ):
        collaborators = Collaborators(sandbox=..., drivers=..., simulator_factory=...,
                                      simulator_llm=llm, judge_llm=llm, grader_factory=...)
        run = await RunExecutor(plan.suite, collaborators, store).execute(plan.run)
"""

from typing import Final

from hecklr_core.errors import (
    BudgetEstimateError,
    BudgetExceededError,
    ConfigError,
    ConfigIssue,
)
from hecklr_core.executor import (
    Collaborators,
    CriterionGrader,
    GraderFactory,
    RetryPolicy,
    RunExecutor,
    is_retryable,
)
from hecklr_core.llm import (
    DEFAULT_PROVIDER_LIMITS,
    ProviderLimit,
    RateLimitedProvider,
    TokenBucket,
    open_llm,
)
from hecklr_core.loading import (
    PERSONA_LIBRARY_GLOB,
    Project,
    Suite,
    SuiteCell,
    config_hash,
    load_project,
    resolve_suite,
)
from hecklr_core.metering import MeteredProvider, RunBudget, TrialSpend
from hecklr_core.planning import (
    CostEstimate,
    RunPlan,
    check_estimate,
    derive_seed,
    estimate_cost,
    expand_matrix,
    extend_matrix,
    make_spec,
    plan_run,
)
from hecklr_core.sink import TrialEventSink
from hecklr_core.storage import ResultStore, RunNotFoundError, RunResults
from hecklr_core.yaml_files import YamlDocument, load_model, load_yaml

__version__ = "0.1.0"

RUNS_DIR: Final = "runs"
"""Runs live in ``<output_dir>/runs/<run_id>/``."""

__all__ = [
    "DEFAULT_PROVIDER_LIMITS",
    "PERSONA_LIBRARY_GLOB",
    "RUNS_DIR",
    "BudgetEstimateError",
    "BudgetExceededError",
    "Collaborators",
    "ConfigError",
    "ConfigIssue",
    "CostEstimate",
    "CriterionGrader",
    "GraderFactory",
    "MeteredProvider",
    "Project",
    "ProviderLimit",
    "RateLimitedProvider",
    "ResultStore",
    "RetryPolicy",
    "RunBudget",
    "RunExecutor",
    "RunNotFoundError",
    "RunPlan",
    "RunResults",
    "Suite",
    "SuiteCell",
    "TokenBucket",
    "TrialEventSink",
    "TrialSpend",
    "YamlDocument",
    "check_estimate",
    "config_hash",
    "derive_seed",
    "estimate_cost",
    "expand_matrix",
    "extend_matrix",
    "is_retryable",
    "load_model",
    "load_project",
    "load_yaml",
    "make_spec",
    "open_llm",
    "plan_run",
    "resolve_suite",
]
