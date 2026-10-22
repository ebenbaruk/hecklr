"""WS13 done criterion: on 1,000 synthetic conversations the miner recovers the planted causes.

Offline (HashingEmbedder, FakeLLM), so it runs on every push. Thresholds:

- detection: >= 98% of planted failures flagged, <= 2% of successful conversations flagged;
- clustering: purity >= 0.95, every one of the 5 planted modes has a cluster of its own, and
  >= 90% of each mode's conversations sit in such clusters;
- synthesis: every planted mode yields a scenario proposal that validates.

The live counterpart (OpenAI embeddings, LLM labels) is ``test_live_recovery.py``.
"""

from pathlib import Path

import anyio
import pytest

from hecklr_llm import FakeLLM
from hecklr_mine import (
    PLANTED_MODES,
    HashingEmbedder,
    MineSettings,
    evaluate_recovery,
    generate_log_set,
    load_logs,
    mine,
)
from hecklr_mine.ingest import LogFormat
from hecklr_schemas import Scenario


def test_ws13_synthetic_log_set_is_reproducible_and_balanced(tmp_path: Path) -> None:
    logs = generate_log_set(1000, seed=5)
    assert generate_log_set(1000, seed=5) == logs
    assert generate_log_set(1000, seed=6) != logs
    modes = list(logs.planted.values())
    assert 0.35 <= sum(mode in PLANTED_MODES for mode in modes) / 1000 <= 0.45
    assert all(modes.count(mode) >= 50 for mode in PLANTED_MODES)
    path = tmp_path / "logs.jsonl"
    logs.write_jsonl(path)
    assert load_logs(path, LogFormat.JSONL) == logs.conversations
    assert "planted" not in path.read_text()


@pytest.mark.parametrize("seed", [0, 1])
def test_ws13_miner_recovers_planted_clusters_on_1000_conversations(
    seed: int, fake_llm: FakeLLM
) -> None:
    logs = generate_log_set(1000, seed=seed)
    settings = MineSettings(world="acme", persona="busy-homeowner", model="fake/m", seed=seed)
    result = anyio.run(
        lambda: mine(logs.conversations, settings, embedder=HashingEmbedder(), llm=fake_llm)
    )
    recovery = evaluate_recovery(
        logs.planted, (r.conversation_id for r in result.failures), result.clusters
    )
    assert recovery.detection_recall >= 0.98
    assert recovery.false_positive_rate <= 0.02
    assert recovery.purity >= 0.95
    assert recovery.recovered == set(PLANTED_MODES)
    assert min(recovery.mode_recall.values()) >= 0.90, recovery.mode_recall

    proposed_clusters = {proposal.cluster_id for proposal in result.proposals}
    for mode, cluster_ids in recovery.clusters_of_mode.items():
        assert proposed_clusters & set(cluster_ids), f"no proposal for {mode}"
    for proposal in result.proposals:
        Scenario.model_validate(proposal.scenario.model_dump())
