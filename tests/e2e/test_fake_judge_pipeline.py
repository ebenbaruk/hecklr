"""Cross-package smoke: a fake judge call flows through router, cache and schemas into a Grade."""

from pathlib import Path

import pytest
from pydantic import BaseModel

from hecklr_llm import CachedProvider, FakeLLM, ProviderRouter, complete_structured
from hecklr_schemas import Grade, GradeId, LLMRequest, Message, TrialId, new_id


class JudgeVerdict(BaseModel):
    resolved: bool
    rationale: str


async def _judge(llm: CachedProvider, trial_id: TrialId) -> Grade:
    verdict, response = await complete_structured(
        llm,
        LLMRequest(
            model="fake/judge",
            system="Decide whether the customer's goal was achieved.",
            messages=[Message(role="user", content="Transcript: ... slot moved to Tuesday 9:00")],
            seed=42,
        ),
        JudgeVerdict,
    )
    return Grade(
        id=GradeId(new_id()),
        trial_id=trial_id,
        grader="judge:resolved",
        grader_version="1",
        value=verdict.resolved,
        passed=verdict.resolved,
        rationale=verdict.rationale,
        judge_model=response.model,
        cost_usd=response.cost_usd,
    )


@pytest.mark.e2e
@pytest.mark.fake
@pytest.mark.anyio
async def test_fake_judge_is_deterministic_and_cached(tmp_path: Path) -> None:
    judge = FakeLLM(rules=[(r"slot moved", JudgeVerdict(resolved=True, rationale="moved"))])
    trial_id = TrialId(new_id())

    async with ProviderRouter(providers={"fake": judge}) as router:
        with CachedProvider(router, tmp_path / "llm.sqlite3") as llm:
            first = await _judge(llm, trial_id)
            second = await _judge(llm, trial_id)

    assert first.passed
    assert first.rationale == "moved"
    assert second.model_dump(exclude={"id"}) == first.model_dump(exclude={"id"})
    assert len(judge.calls) == 1
    assert (llm.misses, llm.hits) == (1, 1)
