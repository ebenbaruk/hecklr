"""A real chat conversation: a local Ollama model plays the agent and another plays the customer.

Opt in with ``pytest -m live``; free, so no budget.
"""

import os
from collections.abc import Sequence

import httpx2
import pytest
from pydantic import TypeAdapter

from hecklr_adapters import make_chat_target
from hecklr_channels import ChatDriver
from hecklr_llm import ProviderRouter
from hecklr_schemas import (
    ChatAdapterConfig,
    DialogueTurn,
    EndReason,
    LLMProvider,
    LLMRequest,
    Message,
    MonotonicClock,
    SimEnd,
    SimTurn,
)

from .fakes import ListSink, brief, context

MODEL = os.environ.get("HECKLR_LIVE_OLLAMA_MODEL", "ollama/qwen3:4b-instruct")
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
CUSTOMER_BRIEF = (
    "You are a customer chatting with a maintenance company's support agent. Your goal: move "
    "Thursday's boiler visit to next Tuesday morning. Write one short message per turn, as the "
    "customer only. Once the agent has agreed to move the visit, reply exactly [END]."
)

pytestmark = [pytest.mark.live, pytest.mark.anyio]


@pytest.fixture(scope="module", autouse=True)
def _require_ollama() -> None:
    try:
        names = {m["id"] for m in httpx2.get(f"{OLLAMA_URL}/models", timeout=2.0).json()["data"]}
    except httpx2.HTTPError:
        names = set()
    if MODEL.removeprefix("ollama/") not in names:
        pytest.skip(f"Ollama at {OLLAMA_URL} with {MODEL} is not available")


class OllamaCustomer:
    def __init__(self, llm: LLMProvider) -> None:
        self.llm = llm

    @property
    def contradictions(self) -> int:
        return 0

    async def next_turn(self, dialogue: Sequence[DialogueTurn]) -> SimTurn | SimEnd:
        # The customer is the "assistant" of its own model call; the agent's messages are "user".
        messages = [
            Message(role="assistant" if turn.speaker == "user" else "user", content=turn.text)
            for turn in dialogue
        ] or [Message(role="user", content="(The chat opens. Write your first message.)")]
        reply = await self.llm.complete(
            LLMRequest(model=MODEL, system=CUSTOMER_BRIEF, messages=messages, seed=1, max_tokens=80)
        )
        text = reply.text.strip()
        if "[END]" in text or not text:
            return SimEnd(reason=EndReason.GOAL_ACHIEVED)
        return SimTurn(text=text)


async def test_live_chat_driver_runs_a_real_conversation_with_streamed_timing() -> None:
    config = TypeAdapter[ChatAdapterConfig](ChatAdapterConfig).validate_python(
        {"type": "openai-compatible", "url": OLLAMA_URL, "model": MODEL.removeprefix("ollama/")}
    )
    sink = ListSink(MonotonicClock())
    async with ProviderRouter() as router:
        result = await ChatDriver(make_chat_target(config)).run(
            context(timeout_s=60), brief(max_turns=8), OllamaCustomer(router), sink
        )

    assert result.status == "ok", result.error
    assert result.end_reason in {EndReason.GOAL_ACHIEVED, EndReason.MAX_TURNS}
    assert result.turns >= 2
    replies = sink.utterances("agent")
    assert replies
    for event, reply in replies:
        assert reply.text
        assert reply.latency_ms is not None
        assert reply.end_ms is not None
        assert reply.end_ms >= event.t_ms
