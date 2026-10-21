"""What the LLM brain has spent, and an optional hard cap on it.

Hecklr's budget covers the simulator and the judges, which it calls itself; the agent's own model
calls happen in another process and Hecklr never sees them. The fault matrix runs the LLM brain on
a paid model under a hard budget, so the agent counts its spend (``GET /healthz`` reports it) and
refuses to call the model once ``cap_usd`` is reached.
"""

from decimal import Decimal

from hecklr_llm import LLMError
from hecklr_schemas import LLMProvider, LLMRequest, LLMResponse


class AgentBudgetError(LLMError):
    """The agent reached its spend cap; the turn fails instead of spending more."""


class SpendMeter:
    """An ``LLMProvider`` that adds up the cost of every completion it forwards."""

    def __init__(self, provider: LLMProvider, cap_usd: Decimal | None = None) -> None:
        """``cap_usd``: refuse every call once this much has been spent (``None``: no cap)."""
        self._provider = provider
        self._cap_usd = cap_usd
        self.spent_usd = Decimal(0)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Forward ``request`` unless the cap is reached, and count what it cost.

        Raises:
            AgentBudgetError: ``spent_usd`` already reached ``cap_usd``.
        """
        if self._cap_usd is not None and self.spent_usd >= self._cap_usd:
            raise AgentBudgetError(
                f"the agent spent ${self.spent_usd} of its ${self._cap_usd} cap (ACME_BUDGET_USD)"
            )
        response = await self._provider.complete(request)
        self.spent_usd += response.cost_usd
        return response
