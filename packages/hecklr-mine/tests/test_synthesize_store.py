"""Stage 5 (WS13 synthesize) and the review queue: mined scenarios load, dedupe, get reviewed."""

from pathlib import Path

import anyio
import pytest

from hecklr_core import load_model
from hecklr_llm import FakeLLM
from hecklr_mine import HashingEmbedder, MineError, decide, load_queue, queued_scenarios
from hecklr_mine.cluster import ClusterLabel, make_cluster
from hecklr_mine.store import (
    PROPOSALS_FILE,
    append_proposals,
    read_proposals,
)
from hecklr_mine.synthesize import (
    deduplicate,
    mined_scenario_id,
    propose,
    synthesize_scenario,
)
from hecklr_schemas import (
    Conversation,
    FailureCluster,
    ProposalStatus,
    Scenario,
    SimulationMode,
)
from hecklr_sim import scenario_to_yaml


def _cluster(label: str = "fails when the customer gives two addresses") -> FailureCluster:
    return make_cluster(ClusterLabel(label=label, description="d"), ["c1", "c2"], ["c1"])


EXEMPLAR = Conversation.model_validate(
    {
        "id": "c1",
        "turns": [
            {"role": "user", "text": "Bills to [ADDRESS_1], the boiler is at [ADDRESS_2]."},
            {"role": "agent", "text": "Booked at [ADDRESS_1].", "tool_calls": [{"tool": "book"}]},
        ],
    }
)


def test_ws13_mined_scenario_ids_are_readable_slugs() -> None:
    cluster = _cluster("Fails when: the customer gives TWO addresses!")
    scenario_id = mined_scenario_id(cluster)
    assert scenario_id.startswith("mined-fails-when-the-customer-gives-two-addresses-")
    assert scenario_id.endswith(cluster.id[-6:].lower())
    assert mined_scenario_id(_cluster("???")).startswith("mined-cluster-")


def test_ws13_synthesized_scenario_is_mined_and_loads_like_an_authored_file(
    tmp_path: Path, fake_llm: FakeLLM
) -> None:
    cluster = _cluster()
    scenario = anyio.run(
        lambda: synthesize_scenario(
            cluster, EXEMPLAR, fake_llm, model="fake/m", world="acme", persona="busy", seed=1
        )
    )
    assert scenario.source is not None
    assert scenario.source.kind == "mined"
    assert scenario.source.cluster_id == cluster.id
    assert scenario.source.conversation_id == "c1"
    assert scenario.source.note == cluster.label
    assert scenario.tags == ["mined", "support"]
    assert scenario.mode is SimulationMode.REPLAY
    assert (scenario.world, scenario.persona) == ("acme", "busy")
    path = tmp_path / f"{scenario.id}.yaml"
    path.write_text(scenario_to_yaml(scenario), encoding="utf-8")
    assert load_model(path, Scenario) == scenario


def _scenario(scenario_id: str, title: str, goal: str) -> Scenario:
    return Scenario.model_validate(
        {
            "id": scenario_id,
            "title": title,
            "channels": ["chat"],
            "persona": "p",
            "world": "w",
            "goal": goal,
            "success": [{"type": "judge", "rubric": "resolved"}],
        }
    )


def test_ws13_dedup_drops_candidates_similar_to_the_suite_or_each_other() -> None:
    existing = [_scenario("move-visit", "Customer moves a boiler visit", "Move the boiler visit")]
    candidates = [
        (_cluster("a"), _scenario("m1", "Customer moves a boiler visit", "Move the boiler visit")),
        (_cluster("b"), _scenario("m2", "Refund a duplicate invoice", "Get a double charge back")),
        (_cluster("c"), _scenario("m3", "Refund a duplicate invoice", "Get a double charge back")),
    ]
    kept, dropped = anyio.run(
        lambda: deduplicate(candidates, existing, HashingEmbedder(), threshold=0.9)
    )
    assert [scenario.id for _, scenario in kept] == ["m2"]
    assert [(d.scenario_id, d.duplicate_of) for d in dropped] == [
        ("m1", "move-visit"),
        ("m3", "m2"),
    ]
    assert all(d.similarity >= 0.9 for d in dropped)
    assert anyio.run(lambda: deduplicate([], existing, HashingEmbedder(), threshold=0.9)) == (
        [],
        [],
    )
    first_only, _ = anyio.run(
        lambda: deduplicate(candidates[1:2], [], HashingEmbedder(), threshold=0.9)
    )
    assert len(first_only) == 1


def _queue(tmp_path: Path) -> tuple[Path, str, str]:
    mine_root = tmp_path / ".hecklr" / "mine"
    first = propose(_cluster("a"), _scenario("mined-a", "A", "Do a"))
    second = propose(_cluster("b"), _scenario("mined-b", "B", "Do b"))
    append_proposals(mine_root / "01RUN" / PROPOSALS_FILE, [first])
    append_proposals(mine_root / "02RUN" / PROPOSALS_FILE, [second])
    return mine_root, first.id, second.id


def test_ws13_review_appends_a_decision_and_the_newest_record_wins(tmp_path: Path) -> None:
    mine_root, first, second = _queue(tmp_path)
    suite = tmp_path / "scenarios" / "mined"
    approved = decide(
        mine_root, first, ProposalStatus.APPROVED, reviewer="ops@acme", scenarios_dir=suite
    )
    assert approved.reviewer == "ops@acme"
    assert approved.decided_at is not None
    assert load_model(suite / "mined-a.yaml", Scenario) == approved.scenario
    decide(mine_root, second, ProposalStatus.REJECTED, reviewer="ops@acme", note="duplicate")
    queue = load_queue(mine_root)
    assert [(p.id, p.status) for p in queue] == [
        (first, ProposalStatus.APPROVED),
        (second, ProposalStatus.REJECTED),
    ]
    # Append-only: both versions of the first proposal are still on disk, in its own run.
    history = read_proposals(mine_root / "01RUN" / PROPOSALS_FILE)
    assert [p.status for p in history] == [ProposalStatus.PROPOSED, ProposalStatus.APPROVED]
    assert [s.id for s in queued_scenarios(mine_root)] == ["mined-a"]


def test_ws13_review_errors(tmp_path: Path) -> None:
    mine_root, first, _ = _queue(tmp_path)
    with pytest.raises(MineError, match="approved or rejected"):
        decide(mine_root, first, ProposalStatus.PROPOSED, reviewer="r")
    with pytest.raises(MineError, match="no proposal"):
        decide(mine_root, "01NOPE", ProposalStatus.APPROVED, reviewer="r")
    decide(mine_root, first, ProposalStatus.REJECTED, reviewer="r")
    with pytest.raises(MineError, match="already rejected"):
        decide(mine_root, first, ProposalStatus.APPROVED, reviewer="r")


def test_ws13_approving_never_overwrites_a_suite_file(tmp_path: Path) -> None:
    mine_root, first, _ = _queue(tmp_path)
    suite = tmp_path / "scenarios"
    suite.mkdir()
    (suite / "mined-a.yaml").write_text("authored: true\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        decide(mine_root, first, ProposalStatus.APPROVED, reviewer="r", scenarios_dir=suite)
    assert load_queue(mine_root)[0].status is ProposalStatus.PROPOSED


def test_ws13_a_corrupt_queue_line_is_reported(tmp_path: Path) -> None:
    path = tmp_path / PROPOSALS_FILE
    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(MineError, match=r"proposals\.jsonl:1"):
        read_proposals(path)
