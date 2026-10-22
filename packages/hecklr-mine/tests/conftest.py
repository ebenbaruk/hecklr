"""Shared fixtures: a FakeLLM that answers every structured request the miner makes."""

import re
from collections.abc import Callable
from typing import Any

import pytest

from hecklr_llm import FakeLLM
from hecklr_schemas import LLMRequest


def miner_reply(request: LLMRequest) -> dict[str, Any]:
    """A plausible structured answer per request type, derived only from the prompt."""
    prompt = request.messages[-1].content
    match request.schema_name:
        case "ClusterLabel":
            first = re.search(r"<summary 1>\n(.*?)\n</summary>", prompt, re.DOTALL)
            words = (first.group(1) if first else "something").split()[3:9]
            return {"label": "fails when " + " ".join(words), "description": "A shared cause."}
        case "ReplayExtraction":
            customer = re.search(r"^CUSTOMER: (.*)$", prompt, re.MULTILINE)
            goal = customer.group(1) if customer else "Get help"
            return {
                "title": f"Customer: {goal[:60]}",
                "goal": goal,
                "facts": [],
                "resolving_tool": None,
                "tags": ["support"],
            }
        case "FailureSummary":
            customer = re.search(r"^CUSTOMER: (.*)$", prompt, re.MULTILINE)
            return {"summary": f"The agent failed: {customer.group(1) if customer else ''}"}
        case "FoundEntities":
            return {"entities": []}
        case _:
            raise AssertionError(f"unexpected request {request.schema_name}")


@pytest.fixture
def responder() -> Callable[[LLMRequest], dict[str, Any]]:
    """``miner_reply``, for tests that wrap or override it."""
    return miner_reply


@pytest.fixture
def fake_llm() -> FakeLLM:
    """A FakeLLM answering label, extraction, summary and PII requests."""
    return FakeLLM(responder=miner_reply)
