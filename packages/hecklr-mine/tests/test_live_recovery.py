"""WS13 live: real embeddings and a real model on the 1,000-conversation synthetic log.

Opt in with ``pytest -m live``. Paid: skipped unless ``OPENAI_API_KEY`` and
``HECKLR_TEST_BUDGET_USD`` (at least $1) are set. One run costs about $0.05 (LLM root-cause
summaries for ~400 failures, ~6 labels and ~6 scenario extractions on ``openai/gpt-6-luna``, plus
``text-embedding-3-small``); the miner's own cap stops it at $1.

Thresholds are looser than the offline test's: model wording varies, so clusters may split.

- detection: >= 98% of planted failures flagged, <= 2% of successes flagged;
- clustering: purity >= 0.90, all 5 planted modes recovered, >= 80% of each mode clustered with it;
- labels start with "fails when" and every planted mode yields a valid proposal.
"""

import os
from decimal import Decimal

import pytest

from hecklr_llm import ProviderRouter
from hecklr_mine import (
    PLANTED_MODES,
    MineSettings,
    OpenAIEmbedder,
    evaluate_recovery,
    generate_log_set,
    mine,
)
from hecklr_schemas import FailureCluster

MODEL = os.environ.get("HECKLR_LIVE_OPENAI_MODEL", "openai/gpt-6-luna")
CAP = Decimal("1.00")

pytestmark = [pytest.mark.live, pytest.mark.anyio, pytest.mark.slow]


@pytest.fixture(scope="module", autouse=True)
def _require_key_and_budget() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY is not set")
    if Decimal(os.environ.get("HECKLR_TEST_BUDGET_USD", "0")) < CAP:
        pytest.skip("HECKLR_TEST_BUDGET_USD does not cover the miner's live test ($1)")


async def test_ws13_live_miner_recovers_planted_clusters_with_real_models() -> None:
    logs = generate_log_set(1000, seed=0)
    settings = MineSettings(
        world="acme",
        persona="busy-homeowner",
        model=MODEL,
        llm_summaries=True,
        max_usd=CAP,
        seed=0,
        concurrency=16,
    )
    embedder = OpenAIEmbedder(api_key=os.environ["OPENAI_API_KEY"])
    async with ProviderRouter() as llm:
        try:
            result = await mine(logs.conversations, settings, embedder=embedder, llm=llm)
        finally:
            await embedder.aclose()

    recovery = evaluate_recovery(
        logs.planted, (r.conversation_id for r in result.failures), result.clusters
    )
    by_id: dict[str, FailureCluster] = {cluster.id: cluster for cluster in result.clusters}
    for mode, cluster_ids in recovery.clusters_of_mode.items():
        labels = [f"{by_id[c].label} ({by_id[c].size})" for c in cluster_ids]
        print(f"{mode}: recall {recovery.mode_recall[mode]:.2f} <- {labels}")
    print(f"purity {recovery.purity:.3f}; spend ${result.llm_usd + result.embedding_usd:.4f}")

    assert recovery.detection_recall >= 0.98
    assert recovery.false_positive_rate <= 0.02
    assert recovery.purity >= 0.90
    assert recovery.recovered == set(PLANTED_MODES)
    assert min(recovery.mode_recall.values()) >= 0.80, recovery.mode_recall
    assert all(cluster.label.lower().startswith("fails when") for cluster in result.clusters)
    proposed = {proposal.cluster_id for proposal in result.proposals}
    for mode, cluster_ids in recovery.clusters_of_mode.items():
        assert proposed & set(cluster_ids), f"no proposal for {mode}"
    assert result.llm_usd + result.embedding_usd < CAP
