"""The agent's own model spend: logged per call and held to an optional cap.

Hecklr meters its simulator and judges; the agent under test pays for its brain separately. A
live fault matrix needs both under one budget, so the agent logs what it has spent and refuses
to call the model once ``ACME_LLM_BUDGET_USD`` is exceeded (its email then goes unanswered, which
the run shows as timeouts: look for ``agent_llm_budget_exhausted`` in the log).

The cap is soft: it is checked before each call, because a call's price is known only from its
response. The call that crosses the cap is paid for, and so is each call already in flight when
it is crossed (one per email being answered concurrently). Set the cap that much below a hard
limit.
"""

from decimal import Decimal

import structlog

from hecklr_llm import LLMError
from hecklr_schemas import LLMProvider, LLMRequest, LLMResponse

_log = structlog.get_logger(__name__)


class SpendCappedProvider:
    """An ``LLMProvider`` that adds up what its responses cost and stops past ``limit_usd``."""

    def __init__(self, inner: LLMProvider, limit_usd: Decimal | None) -> None:
        self._inner = inner
        self.limit_usd = limit_usd
        self.spent_usd = Decimal(0)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Complete ``request`` unless the cap is already exceeded.

        Raises:
            LLMError: the agent has spent more than ``limit_usd``.
        """
        if self.limit_usd is not None and self.spent_usd > self.limit_usd:
            _log.error("agent_llm_budget_exhausted", spent_usd=str(self.spent_usd))
            raise LLMError(f"agent spent ${self.spent_usd} of its ${self.limit_usd} budget")
        response = await self._inner.complete(request)
        self.spent_usd += response.cost_usd
        _log.info("agent_llm_call", cost_usd=str(response.cost_usd), spent_usd=str(self.spent_usd))
        return response
