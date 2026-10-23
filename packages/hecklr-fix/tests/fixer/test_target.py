from datetime import UTC, datetime

import pytest

from hecklr_fix.errors import FixError
from hecklr_fix.target import mined_target, target_from_run
from hecklr_schemas import (
    Channel,
    ClusterId,
    FailureCluster,
    ProposalId,
    ProposalStatus,
    ScenarioProposal,
    new_id,
)

from .factories import make_run, scenario

SCENARIOS = {
    s.id: s
    for s in (
        scenario("book-a", ["booking", "core"]),
        scenario("book-b", ["booking"]),
        scenario("refund-a", ["refunds"]),
    )
}
NOW = datetime(2026, 10, 23, tzinfo=UTC)


def test_us10_a_failing_run_becomes_a_cluster_of_its_failing_scenarios() -> None:
    run = make_run({"book-a": [False, True], "book-b": [True, True], "refund-a": [False, None]})

    target = target_from_run(run, SCENARIOS, {}, channels=[Channel.CHAT], personas_glob="p/*")

    assert [s.id for s in target.scenarios] == ["book-a", "refund-a"]
    cluster = target.cluster
    assert cluster.source == "run"
    assert cluster.label == "fails book-a, refund-a"
    failing = {t.id for t in run.trials if t.scenario_id != "book-b" and t.index == 0}
    assert set(cluster.trial_ids) == failing
    assert set(cluster.exemplar_trial_ids) == failing
    assert (target.channels, target.personas) == ((Channel.CHAT,), "p/*")


def test_against_a_baseline_only_scenarios_that_dropped_are_kept() -> None:
    baseline = make_run({"book-a": [True, True], "book-b": [False, False], "refund-a": [True]})
    candidate = make_run({"book-a": [False, True], "book-b": [False, False], "new": [False]})

    target = target_from_run(candidate, SCENARIOS, {}, baseline=baseline)

    assert [s.id for s in target.scenarios] == ["book-a"]
    assert target.cluster.description is not None
    assert f"dropped against run {baseline.run.id}" in target.cluster.description


def test_tag_and_scenario_filters_narrow_the_cluster() -> None:
    run = make_run({"book-a": [False], "book-b": [False], "refund-a": [False]})

    by_tag = target_from_run(run, SCENARIOS, {}, tags=["booking"])
    by_id = target_from_run(run, SCENARIOS, {}, tags=["booking"], scenario_ids=["book-b"])

    assert [s.id for s in by_tag.scenarios] == ["book-a", "book-b"]
    assert [s.id for s in by_id.scenarios] == ["book-b"]
    assert by_id.cluster.description is not None
    assert "(tagged booking; among book-b)" in by_id.cluster.description


def test_many_failing_scenarios_get_a_counted_label() -> None:
    scenarios = {f"s{i}": scenario(f"s{i}") for i in range(4)}
    run = make_run({sid: [False] for sid in scenarios})

    assert target_from_run(run, scenarios, {}).cluster.label == "fails 4 scenarios"


def test_us10_nothing_failing_is_reported_not_guessed() -> None:
    run = make_run({"book-a": [True], "refund-a": [False]})

    with pytest.raises(FixError, match=r"no scenario of run .* failed \(tagged booking\)"):
        target_from_run(run, SCENARIOS, {}, tags=["booking"])


def _cluster() -> FailureCluster:
    return FailureCluster(
        id=ClusterId(new_id()),
        label="fails when the customer wants mornings",
        source="production",
        conversation_ids=["c1"],
        created_at=NOW,
    )


def _proposal(cluster: FailureCluster, scenario_id: str, **decision: object) -> ScenarioProposal:
    return ScenarioProposal.model_validate(
        {
            "id": ProposalId(new_id()),
            "cluster_id": cluster.id,
            "scenario": scenario(scenario_id).model_dump(),
            "created_at": NOW,
            **decision,
        }
    )


def test_a_mined_cluster_runs_its_proposed_and_approved_scenarios() -> None:
    cluster, other = _cluster(), _cluster()
    decided = {"reviewer": "eli", "decided_at": NOW}
    kept = _proposal(cluster, "mined-a")
    kept = kept.model_copy(update={"status": ProposalStatus.APPROVED, **decided})
    rejected = _proposal(cluster, "mined-b")
    rejected = rejected.model_copy(update={"status": ProposalStatus.REJECTED, **decided})
    proposed = _proposal(cluster, "mined-c")

    target = mined_target(
        [other, cluster], [kept, rejected, _proposal(other, "other"), proposed], cluster.id
    )

    assert target.cluster == cluster
    assert [s.id for s in target.scenarios] == ["mined-a", "mined-c"]


def test_mined_cluster_errors_name_the_problem() -> None:
    cluster = _cluster()
    with pytest.raises(FixError, match="no cluster"):
        mined_target([], [], cluster.id)
    rejected = _proposal(cluster, "mined-a").model_copy(
        update={"status": ProposalStatus.REJECTED, "reviewer": "eli", "decided_at": NOW}
    )
    with pytest.raises(FixError, match="no proposed or approved scenario"):
        mined_target([cluster], [rejected], cluster.id)
