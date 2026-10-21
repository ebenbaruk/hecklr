"""The LLM brain's own spend: counted, reported on /healthz and capped (ACME_BUDGET_USD)."""

from decimal import Decimal

import httpx2
import pytest
from acme_agent import AcmeAgent, AgentConfigError, AgentSettings, create_app, load_agent_files
from acme_agent.__main__ import parse_args, settings_from
from acme_agent.llm_brain import AgentStep
from acme_agent.spend import AgentBudgetError, SpendMeter
from acme_agent.tools import LocalWorldTools

from hecklr_llm import FakeLLM
from hecklr_schemas import LLMRequest, LLMResponse, Message, World

pytestmark = pytest.mark.anyio

REPLY = AgentStep(action="reply", tool="", arguments={}, message="Hello!").model_dump_json()


class PricedLLM:
    """FakeLLM's replies at a fixed price per call."""

    def __init__(self, price: str) -> None:
        self._fake = FakeLLM(responder=lambda _: REPLY)
        self._price = Decimal(price)
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        response = await self._fake.complete(request)
        return response.model_copy(update={"cost_usd": self._price})


def request() -> LLMRequest:
    return LLMRequest(model="fake/agent", messages=[Message(role="user", content="hi")])


async def test_the_meter_adds_up_what_each_call_cost() -> None:
    meter = SpendMeter(PricedLLM("0.002"))
    for _ in range(3):
        await meter.complete(request())
    assert meter.spent_usd == Decimal("0.006")


async def test_the_meter_refuses_calls_once_the_cap_is_reached() -> None:
    provider = PricedLLM("0.004")
    meter = SpendMeter(provider, cap_usd=Decimal("0.01"))
    for _ in range(3):
        await meter.complete(request())
    with pytest.raises(AgentBudgetError, match=r"\$0\.012 of its \$0\.01 cap"):
        await meter.complete(request())
    assert provider.calls == 3


async def test_healthz_reports_the_llm_brains_spend(world: World) -> None:
    settings = AgentSettings(brain="llm", model="fake/agent")
    agent = AcmeAgent(load_agent_files(), settings, PricedLLM("0.001"))
    app = create_app(agent, world)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://agent") as client:
        assert (await client.get("/healthz")).json()["spent_usd"] == "0"
        await agent.respond("s", "Hello", lambda: LocalWorldTools(world))
        health = (await client.get("/healthz")).json()
    assert Decimal(health["spent_usd"]) == Decimal("0.001")


def test_the_rules_brain_spends_nothing() -> None:
    assert AcmeAgent(load_agent_files(), AgentSettings(brain="rules")).spent_usd == 0


def test_the_cap_comes_from_the_environment_or_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    assert AgentSettings.from_env({}).budget_usd is None
    assert AgentSettings.from_env({"ACME_BUDGET_USD": "2.5"}).budget_usd == Decimal("2.5")
    monkeypatch.setenv("ACME_BUDGET_USD", "9")
    assert settings_from(parse_args(["--budget-usd", "0.75"])).budget_usd == Decimal("0.75")


@pytest.mark.parametrize("value", ["-1", "lots", "NaN", "Infinity"])
def test_a_bad_cap_is_refused(value: str) -> None:
    with pytest.raises(AgentConfigError, match="invalid ACME_"):
        AgentSettings.from_env({"ACME_BUDGET_USD": value})
