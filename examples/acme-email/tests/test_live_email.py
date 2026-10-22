"""The email agent with its LLM brain on a real model. Opt in with ``pytest -m live``.

The default model is ``openai/gpt-6-luna`` (ADR 0007d): it needs ``OPENAI_API_KEY`` and
``HECKLR_TEST_BUDGET_USD``; the file costs about a cent. ``ACME_LIVE_MODEL`` picks another model,
e.g. ``ollama/mistral:latest`` for a free local run (skipped when Ollama or the model is missing).
"""

import os
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx2
import pytest
from acme_agent import AgentSettings
from acme_agent.config import DEFAULT_MODEL
from email_testing import EmailTrial, email_desk, load_scenarios, run_email_trial

from hecklr_llm import ProviderRouter
from hecklr_schemas import LLMRequest, LLMResponse, Persona, World

MODEL = os.environ.get("ACME_LIVE_MODEL", DEFAULT_MODEL)
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
MAX_COST_PER_TEST = Decimal("0.02")
LLM_REPLY_TIMEOUT_S = 90.0
"""A luna turn with a few tool steps takes 5-30 s; a silent agent still misses this."""
SCENARIOS = {s.id: s for s in load_scenarios("scripted/*.yaml")}

pytestmark = [pytest.mark.live, pytest.mark.anyio]


def _ollama_has_model() -> bool:
    try:
        response = httpx2.get(f"{OLLAMA_URL}/models", timeout=2.0)
    except httpx2.HTTPError:
        return False
    names = {entry["id"] for entry in response.json().get("data", [])}
    return MODEL.removeprefix("ollama/") in names


@pytest.fixture(scope="module", autouse=True)
def _require_model() -> None:
    if MODEL.startswith("ollama/"):
        if not _ollama_has_model():
            pytest.skip(f"Ollama at {OLLAMA_URL} with {MODEL} is not available")
        return
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip(f"OPENAI_API_KEY is not set, so {MODEL} cannot be called")
    if Decimal(os.environ.get("HECKLR_TEST_BUDGET_USD", "0")) < MAX_COST_PER_TEST * 3:
        pytest.skip("HECKLR_TEST_BUDGET_USD does not cover the live email tests")


class MeteredRouter(ProviderRouter):
    """A router that adds up what its calls cost, so each test can check its spend."""

    spent = Decimal(0)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        response = await super().complete(request)
        self.spent += response.cost_usd
        return response


@pytest.fixture
async def llm() -> AsyncIterator[MeteredRouter]:
    async with MeteredRouter() as router:
        yield router
    assert router.spent <= MAX_COST_PER_TEST


async def _trial(
    variant: str, scenario_id: str, llm: ProviderRouter, personas: dict[str, Persona], world: World
) -> EmailTrial:
    scenario = SCENARIOS[scenario_id]
    desk = email_desk(variant, settings=AgentSettings(brain="llm", model=MODEL), llm=llm)
    persona = personas[scenario.persona_ids[0]]
    return await run_email_trial(
        scenario, persona, world, desk, reply_timeout_s=LLM_REPLY_TIMEOUT_S
    )


async def test_llm_email_agent_moves_a_visit_in_thread(
    llm: MeteredRouter, personas: dict[str, Persona], world: World
) -> None:
    trial = await _trial("clean", "email-reschedule-2", llm, personas, world)
    assert trial.passed, trial.failed()


async def test_llm_email_agent_answers_although_the_ticket_tool_hangs(
    llm: MeteredRouter, personas: dict[str, Persona], world: World
) -> None:
    trial = await _trial("clean", "email-timeout-emergency-1", llm, personas, world)
    assert trial.trial.status == "ok", trial.trial.error
    [reply] = trial.emails("agent")
    assert reply.message.in_reply_to is not None


async def test_sc8_f12_llm_email_agent_goes_silent_when_the_ticket_tool_hangs(
    llm: MeteredRouter, personas: dict[str, Persona], world: World
) -> None:
    trial = await _trial("F12", "email-timeout-emergency-1", llm, personas, world)
    assert trial.trial.status == "timeout"
    assert trial.emails("agent") == []
