"""Errors the orchestrator raises to its callers (the CLI maps them to messages and exit codes)."""

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ConfigIssue:
    """One problem in an authored file, located as precisely as the YAML allows."""

    file: Path
    line: int | None
    """1-based line of the offending node; ``None`` when the problem is not tied to one node."""
    message: str

    def __str__(self) -> str:
        location = f"{self.file}:{self.line}" if self.line is not None else str(self.file)
        return f"{location}: {self.message}"


class ConfigError(Exception):
    """An authored file (config, scenario, persona or world) is invalid; issues name the file."""

    def __init__(self, issues: list[ConfigIssue]) -> None:
        if not issues:
            raise ValueError("a ConfigError needs at least one issue")
        self.issues = issues
        super().__init__("\n".join(str(issue) for issue in issues))


class BudgetEstimateError(Exception):
    """The estimated cost of a planned run exceeds ``budget.usd``; the run is refused."""

    def __init__(self, estimate_usd: Decimal, budget_usd: Decimal, trials: int) -> None:
        self.estimate_usd = estimate_usd
        self.budget_usd = budget_usd
        super().__init__(
            f"estimated cost ${estimate_usd:.2f} for {trials} trials exceeds the budget of "
            f"${budget_usd:.2f}; raise budget.usd (or --budget-usd), or run fewer trials"
        )


class BudgetExceededError(Exception):
    """Actual model spend crossed ``budget.usd`` during a run; no further model call is allowed."""
