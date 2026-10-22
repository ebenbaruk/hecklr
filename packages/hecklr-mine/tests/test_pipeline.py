"""The miner end to end on FakeLLM (US-08): logs in, clusters and queued proposals out."""

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import anyio
import pytest

from hecklr_core import load_model
from hecklr_llm import FakeLLM
from hecklr_mine import (
    BudgetExceededError,
    HashingEmbedder,
    MineResult,
    MineSettings,
    generate_log_set,
    load_queue,
    mine,
    read_summary,
    summarize,
    write_run,
)
from hecklr_mine.store import (
    CLUSTERS_FILE,
    CONVERSATIONS_FILE,
    FAILURES_FILE,
    SCENARIOS_DIR,
)
from hecklr_schemas import FailureCluster, LLMRequest, LLMResponse, Scenario

Reply = Callable[[LLMRequest], dict[str, Any]]
SETTINGS = MineSettings(world="acme", persona="busy-homeowner", model="fake/miner", seed=3)


def _run(settings: MineSettings, llm: Any, n: int = 300, **kwargs: Any) -> MineResult:
    logs = generate_log_set(n, seed=11)
    return anyio.run(
        lambda: mine(logs.conversations, settings, embedder=HashingEmbedder(), llm=llm, **kwargs)
    )


def test_us08_mine_turns_logs_into_labelled_clusters_and_proposals(
    tmp_path: Path, fake_llm: FakeLLM
) -> None:
    result = _run(SETTINGS, fake_llm)
    assert len(result.conversations) == 300
    assert result.clusters
    assert all(c.label.startswith("fails when") for c in result.clusters)
    assert len(result.proposals) + len(result.duplicates) + len(result.synthesis_errors) == len(
        result.clusters
    )
    clustered = {cid for c in result.clusters for cid in c.conversation_ids}
    assert clustered == {r.conversation_id for r in result.failures if r.cluster_id}
    # PII never reaches the artifacts.
    dumped = "".join(c.model_dump_json() for c in result.conversations)
    assert "@example.com" not in dumped
    assert result.placeholders["EMAIL"] > 0

    run_dir = tmp_path / "mine" / "01RUN"
    summary = summarize(
        result, mine_id="01RUN", source="logs.jsonl", log_format="jsonl", model="fake/miner"
    )
    write_run(run_dir, result, summary)
    assert read_summary(run_dir) == summary
    assert summary.failures == len(result.failures)
    assert summary.clusters == len(result.clusters)
    assert summary.noise == sum(r.cluster_id is None for r in result.failures)
    assert set(summary.signals) <= {"escalation", "abandonment", "repeated_question",
                                    "negative_sentiment"}  # fmt: skip
    assert len((run_dir / CONVERSATIONS_FILE).read_text().splitlines()) == 300
    assert len((run_dir / FAILURES_FILE).read_text().splitlines()) == summary.failures
    clusters = [
        FailureCluster.model_validate_json(line)
        for line in (run_dir / CLUSTERS_FILE).read_text().splitlines()
    ]
    assert clusters == result.clusters
    for proposal in load_queue(tmp_path / "mine"):
        path = run_dir / SCENARIOS_DIR / f"{proposal.scenario.id}.yaml"
        assert load_model(path, Scenario) == proposal.scenario
    with pytest.raises(FileExistsError):
        write_run(run_dir, result, summary)


def test_ws13_without_a_model_the_miner_clusters_with_keyword_labels(fake_llm: FakeLLM) -> None:
    result = _run(MineSettings(world="w", persona="p"), None)
    assert result.clusters
    assert all(c.label.startswith("failures about") for c in result.clusters)
    assert result.proposals == []
    assert result.llm_usd == 0


def test_ws13_llm_scrub_and_summaries_go_through_the_model(fake_llm: FakeLLM) -> None:
    settings = MineSettings(
        world="w", persona="p", model="fake/m", llm_scrub=True, llm_summaries=True
    )
    result = _run(settings, fake_llm, n=60)
    kinds = {call.schema_name for call in fake_llm.calls}
    assert {"FoundEntities", "FailureSummary"} <= kinds
    assert all(r.summary.startswith("The agent failed:") for r in result.failures)


def test_ws13_existing_scenarios_deduplicate_proposals(fake_llm: FakeLLM) -> None:
    first = _run(SETTINGS, fake_llm)
    again = _run(SETTINGS, fake_llm, existing=[p.scenario for p in first.proposals])
    assert again.proposals == []
    assert len(again.duplicates) == len(first.proposals)


def test_ws13_unusable_model_replies_fall_back_or_are_recorded(responder: Reply) -> None:
    def broken(request: LLMRequest) -> Any:
        if request.schema_name == "ClusterLabel":
            return "not json"
        if request.schema_name == "ReplayExtraction":
            return {**responder(request), "goal": " "}
        return responder(request)

    result = _run(SETTINGS, FakeLLM(responder=broken))
    assert all(c.label.startswith("failures about") for c in result.clusters)
    assert result.proposals == []
    assert set(result.synthesis_errors) == {c.id for c in result.clusters}


class _Paid:
    """FakeLLM replies that cost one cent each."""

    def __init__(self, responder: Reply) -> None:
        self._fake = FakeLLM(responder=responder)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        response = await self._fake.complete(request)
        return response.model_copy(update={"cost_usd": Decimal("0.01")})


def test_ws13_spend_is_counted_and_capped(responder: Reply) -> None:
    result = _run(SETTINGS, _Paid(responder))
    assert result.llm_usd == Decimal("0.01") * (2 * len(result.clusters))
    capped = MineSettings(world="w", persona="p", model="fake/m", max_usd=Decimal("0.02"))
    with pytest.raises(BudgetExceededError, match="reached the cap"):
        _run(capped, _Paid(responder))


@pytest.mark.parametrize(
    ("settings", "llm", "message"),
    [
        ({"llm_scrub": True}, None, "need a model"),
        ({"rubrics": ("resolved",)}, None, "need a judge_model"),
        ({"model": "fake/m"}, None, "no LLM provider"),
    ],
)
def test_ws13_inconsistent_settings_are_rejected(
    settings: dict[str, Any], llm: Any, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _run(MineSettings(world="w", persona="p", **settings), llm, n=5)


def test_ws13_judges_run_inside_the_pipeline(responder: Reply) -> None:
    def judging(request: LLMRequest) -> Any:
        if request.schema_name == "JudgeVerdict":
            return {"verdict": "fail", "rationale": "unresolved", "evidence": []}
        return responder(request)

    llm = FakeLLM(responder=judging)
    settings = MineSettings(world="w", persona="p", judge_model="fake/j", rubrics=("resolved",))
    result = _run(settings, llm, n=20)
    assert all(r.failed for r in result.reports)
    assert any(s.kind.value == "judge" for r in result.reports for s in r.signals)
    json.loads(result.reports[0].model_dump_json())
