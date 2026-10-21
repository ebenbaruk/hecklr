"""Built-in judges against a real local model (Ollama). Opt in with ``pytest -m live``; free."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import httpx2
import pytest

from hecklr_grade import JudgeGrader, load_rubric
from hecklr_llm import ProviderRouter

if TYPE_CHECKING:
    from conftest import ContextFactory, EventLog

MODEL = os.environ.get("HECKLR_LIVE_OLLAMA_MODEL", "ollama/qwen3:4b-instruct")
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")

pytestmark = [pytest.mark.live, pytest.mark.anyio]


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


def _invented_fee(log: EventLog) -> str:
    log.say("user", "Can I cancel my boiler visit on Friday?", 0)
    log.call("get_policy", {"policy_id": "cancellation"}, 500, result={"fee_eur": 0})
    return log.say("agent", "Yes, it's cancelled. There is a 45 euro cancellation fee.", 1_000).id


async def test_live_no_hallucination_judge_catches_an_invented_fee(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    claim_id = _invented_fee(log)
    async with ProviderRouter() as router:
        ctx = ctx_for(
            log, {"type": "judge", "rubric": "no_hallucination"}, llm=router, judge_model=MODEL
        )
        grade = await JudgeGrader(load_rubric("no_hallucination")).grade(ctx)
    assert grade.status == "ok", grade.rationale
    assert not grade.passed, grade.rationale
    assert all(ref.event_id in {event.id for event in log.events} for ref in grade.evidence)
    assert claim_id in {ref.event_id for ref in grade.evidence} or grade.evidence == []


async def test_live_majority_vote_returns_three_valid_votes(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    _invented_fee(log)
    async with ProviderRouter() as router:
        ctx = ctx_for(
            log,
            {"type": "judge", "rubric": "tone", "n_judges": 3},
            llm=router,
            judge_model=MODEL,
        )
        grade = await JudgeGrader(load_rubric("tone")).grade(ctx)
    assert grade.status == "ok", grade.rationale
    assert len(grade.votes) == 3
