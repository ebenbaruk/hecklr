"""Real model calls against a local Ollama. Opt in with ``pytest -m live``; free, so no budget."""

import os

import httpx2
import pytest
from pydantic import BaseModel

from hecklr_llm import LLMRequest, Message, ProviderRouter, complete_structured

MODEL = os.environ.get("HECKLR_LIVE_OLLAMA_MODEL", "ollama/qwen3:4b-instruct")
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")

pytestmark = pytest.mark.live


def _ollama_has_model() -> bool:
    try:
        response = httpx2.get(f"{OLLAMA_URL}/models", timeout=2.0)
    except httpx2.HTTPError:
        return False
    names = {entry["id"] for entry in response.json().get("data", [])}
    return MODEL.removeprefix("ollama/") in names


@pytest.fixture(scope="module", autouse=True)
def _require_ollama() -> None:
    if not _ollama_has_model():
        pytest.skip(f"Ollama at {OLLAMA_URL} with {MODEL} is not available")


class Booking(BaseModel):
    weekday: str
    morning: bool


@pytest.mark.anyio
async def test_live_ollama_plain_completion() -> None:
    async with ProviderRouter() as router:
        reply = await router.complete(
            LLMRequest(
                model=MODEL,
                system="Answer with one lowercase word and nothing else.",
                messages=[Message(role="user", content="What is the opposite of 'ping'?")],
                temperature=0.0,
                seed=1,
                max_tokens=20,
            )
        )

    assert "pong" in reply.text.lower()
    assert reply.usage.output_tokens > 0
    assert reply.cost_usd == 0


@pytest.mark.anyio
async def test_live_ollama_structured_completion() -> None:
    async with ProviderRouter() as router:
        booking, _ = await complete_structured(
            router,
            LLMRequest(
                model=MODEL,
                messages=[
                    Message(
                        role="user",
                        content="The customer wants Tuesday at 9am. Extract weekday and morning.",
                    )
                ],
                temperature=0.0,
                seed=1,
                max_tokens=200,
            ),
            Booking,
        )

    assert booking.weekday.lower().startswith("tue")
    assert booking.morning is True
