"""Hecklr orchestrator: loading, matrix expansion, budgets, trial execution and storage."""

from hecklr_core.errors import (
    BudgetEstimateError,
    BudgetExceededError,
    ConfigError,
    ConfigIssue,
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

__all__ = [
    "DEFAULT_PROVIDER_LIMITS",
    "PERSONA_LIBRARY_GLOB",
    "BudgetEstimateError",
    "BudgetExceededError",
    "ConfigError",
    "ConfigIssue",
    "CostEstimate",
    "MeteredProvider",
    "Project",
    "ProviderLimit",
    "RateLimitedProvider",
    "ResultStore",
    "RunBudget",
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
    "load_model",
    "load_project",
    "load_yaml",
    "make_spec",
    "open_llm",
    "plan_run",
    "resolve_suite",
]
