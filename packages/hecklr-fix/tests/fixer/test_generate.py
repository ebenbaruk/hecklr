import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hecklr_fix.generate import failure_evidence, generate_candidates
from hecklr_fix.patching import ProposedEdit, ProposedPatch
from hecklr_fix.target import FixTarget
from hecklr_llm import FakeLLM, LLMProviderError
from hecklr_schemas import ClusterId, FailureCluster, LLMRequest, LLMResponse, new_id

from .factories import make_run, scenario, suite_run

pytestmark = pytest.mark.anyio

FILES = {
    "prompts/system.md": "Rules:\n- Book the first free slot.\n",
    "tools/descriptions.yaml": "tools: {}\n",
}
TARGET = FixTarget(
    cluster=FailureCluster(
        id=ClusterId(new_id()),
        label="books without confirming",
        description="Scenarios of run X that failed",
        source="run",
        created_at=datetime.now(UTC),
    ),
    scenarios=(scenario("book-a"),),
)


def _edit(file: str, find: str, replace: str, rationale: str = "r") -> ProposedPatch:
    return ProposedPatch(
        rationale=rationale, edits=[ProposedEdit(file=file, find=find, replace=replace)]
    )


GOOD = _edit(
    "prompts/system.md",
    "Book the first free slot.",
    "Before booking, confirm the exact slot.",
    "The prompt books without asking.",
)


async def test_us10_candidates_are_independent_seeded_requests_and_bad_ones_are_dropped() -> None:
    llm = FakeLLM(
        script=[
            GOOD,
            _edit("scenarios/book-a.yaml", "x", "y"),  # guardrail: not editable
            _edit("prompts/system.md", "absent text", "y"),
            GOOD,  # the same patch again
            "not json at all",
        ]
    )

    generation = await generate_candidates(
        llm, model="fake/fixer", target=TARGET, files=FILES, evidence="EVIDENCE", count=5, seed=3
    )

    assert [c.index for c in generation.candidates] == [0]
    good = generation.candidates[0]
    assert good.files == {
        "prompts/system.md": "Rules:\n- Before booking, confirm the exact slot.\n"
    }
    assert good.diff.startswith("diff --git a/prompts/system.md b/prompts/system.md\n")
    assert good.model == "fake/fixer"
    reasons = {r.index: r.reason for r in generation.rejected}
    assert "touches 'scenarios/book-a.yaml', which is not editable" in reasons[1]
    assert "not found in prompts/system.md" in reasons[2]
    assert reasons[3] == "same patch as candidate 0"
    assert "unusable reply" in reasons[4]
    seeds = [call.seed for call in llm.calls]
    assert len(set(seeds)) == 5
    prompt = llm.calls[0].messages[0].content
    assert "books without confirming" in prompt
    assert "EVIDENCE" in prompt
    assert "## tools/descriptions.yaml" in prompt
    assert "proposal 1 of 5" in prompt
    assert llm.calls[0].json_schema is not None


async def test_generation_counts_the_cost_of_unusable_replies() -> None:
    class Paid:
        async def complete(self, request: LLMRequest) -> LLMResponse:
            return LLMResponse(
                model=request.model, text="{}", cost_usd=Decimal("0.01"), truncated=True
            )

    generation = await generate_candidates(
        Paid(), model="fake/fixer", target=TARGET, files=FILES, evidence="", count=2, seed=1
    )

    assert generation.cost_usd == Decimal("0.02")
    assert all("cut off" in r.reason for r in generation.rejected)


async def test_a_reply_missing_fields_is_rejected_with_the_reason() -> None:
    llm = FakeLLM(script=[json.dumps({"rationale": "r", "edits": []})])

    generation = await generate_candidates(
        llm, model="fake/fixer", target=TARGET, files=FILES, evidence="", count=1, seed=1
    )

    assert "the reply is not a patch" in generation.rejected[0].reason


async def test_a_provider_outage_is_not_a_rejected_candidate() -> None:
    class Down:
        async def complete(self, request: LLMRequest) -> LLMResponse:
            raise LLMProviderError("503", retryable=True)

    with pytest.raises(LLMProviderError):
        await generate_candidates(
            Down(), model="fake/fixer", target=TARGET, files=FILES, evidence="", count=1, seed=1
        )


def test_evidence_shows_failing_trials_round_robin_with_causes_and_transcripts() -> None:
    run = suite_run(
        make_run({"book-a": [False, False, False], "book-b": [True, False]}),
        [scenario("book-a"), scenario("book-b")],
    )

    evidence = failure_evidence(run, max_trials=2)

    assert evidence.count("## Scenario") == 2
    assert "## Scenario book-a (trial 0)" in evidence
    assert "## Scenario book-b (trial 1)" in evidence
    assert "Customer goal: Goal of book-a" in evidence
    assert "- judge:resolved: booked a slot the customer never chose" in evidence
    assert "customer: Book me Tuesday please" in evidence
    assert "agent: Booked Monday 9:00." in evidence


def test_long_transcripts_keep_their_end() -> None:
    results = make_run({"book-a": [False]})
    long_text = "word " * 3_000
    event = results.events[0]
    results.events[0] = event.model_copy(
        update={"payload": event.payload.model_copy(update={"text": long_text})}
    )

    evidence = failure_evidence(suite_run(results, [scenario("book-a")]), max_trials=1)

    assert "[...]\n" in evidence
    assert "Booked Monday 9:00." in evidence
