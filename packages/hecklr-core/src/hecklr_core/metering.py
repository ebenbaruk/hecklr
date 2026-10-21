"""Cost attribution: every model call of a trial is charged to the trial and to the run budget."""

from decimal import Decimal

from hecklr_core.errors import BudgetExceededError
from hecklr_schemas import LLMProvider, LLMRequest, LLMResponse


class RunBudget:
    """Actual spend of a run against its hard cap.

    Spend comes from ``LLMResponse.cost_usd`` (zero for cache hits), so it is what was really
    paid. The cap is crossed when spend goes strictly above ``limit_usd``.
    """

    def __init__(self, limit_usd: Decimal | None, spent_usd: Decimal = Decimal(0)) -> None:
        self.limit_usd = limit_usd
        self.spent_usd = spent_usd

    @property
    def exceeded(self) -> bool:
        """Whether spend has crossed the cap; no further model call is allowed once it has."""
        return self.limit_usd is not None and self.spent_usd > self.limit_usd

    def ensure_available(self) -> None:
        """Raise ``BudgetExceededError`` if the cap has been crossed."""
        if self.exceeded:
            raise BudgetExceededError(
                f"run spent ${self.spent_usd} of its ${self.limit_usd} budget"
            )

    def charge(self, amount_usd: Decimal) -> None:
        """Record spend; raise ``BudgetExceededError`` if it crosses the cap."""
        self.spent_usd += amount_usd
        self.ensure_available()


class TrialSpend:
    """What one trial has spent so far, across simulator and judge calls and every attempt."""

    def __init__(self, budget: RunBudget) -> None:
        self.budget = budget
        self.total_usd = Decimal(0)

    def charge(self, amount_usd: Decimal) -> None:
        """Attribute spend to the trial and the run."""
        self.total_usd += amount_usd
        self.budget.charge(amount_usd)


class MeteredProvider:
    """An ``LLMProvider`` that charges every response to one trial.

    It refuses to send a request once the run budget is exhausted, and raises after a response
    that crosses the cap so the orchestrator stops the run. That response's cost is still
    counted: it was paid.
    """

    def __init__(self, inner: LLMProvider, spend: TrialSpend) -> None:
        self._inner = inner
        self._spend = spend

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Complete ``request`` through the wrapped provider and charge its cost."""
        self._spend.budget.ensure_available()
        response = await self._inner.complete(request)
        self._spend.charge(response.cost_usd)
        return response
