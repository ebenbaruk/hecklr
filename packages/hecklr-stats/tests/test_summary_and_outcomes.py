import pytest

from hecklr_schemas import IntervalMethod, MetricSummary, RunId, new_id
from hecklr_stats.outcomes import TrialOutcome, slice_key, slices_of
from hecklr_stats.summary import summarize_run

RUN = RunId(new_id())


def _trial(
    scenario: str,
    passed: bool,
    *,
    channel: str = "chat",
    tags: frozenset[str] = frozenset(),
    latencies: tuple[int, ...] = (),
) -> TrialOutcome:
    return TrialOutcome(
        scenario_id=scenario,
        passed=passed,
        labels={"channel": channel},
        tags=tags,
        latencies_ms=latencies,
    )


def _by_key(summaries: list[MetricSummary]) -> dict[tuple[str, str], MetricSummary]:
    return {(s.metric, slice_key(s.slice)): s for s in summaries}


def test_trial_outcome_slices_and_membership() -> None:
    outcome = _trial("s1", True, channel="voice", tags=frozenset({"billing", "adversarial"}))
    assert outcome.slices() == [
        {"channel": "voice"},
        {"tag": "adversarial"},
        {"tag": "billing"},
    ]
    assert outcome.in_slice({})
    assert outcome.in_slice({"tag": "billing"})
    assert not outcome.in_slice({"tag": "scheduling"})
    assert not outcome.in_slice({"channel": "chat"})
    assert outcome.in_slice({"channel": "voice", "tag": "billing"})


@pytest.mark.parametrize("key", ["tag", "scenario"])
def test_trial_outcome_rejects_reserved_label_keys(key: str) -> None:
    with pytest.raises(ValueError, match="reserved"):
        TrialOutcome(scenario_id="s", passed=True, labels={key: "x"})


def test_trial_outcome_rejects_negative_latency() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        TrialOutcome(scenario_id="s", passed=True, latencies_ms=(-1,))


def test_slices_of_unions_runs_and_slice_key_is_stable() -> None:
    first = [_trial("a", True, channel="chat")]
    second = [_trial("a", True, channel="voice", tags=frozenset({"x"}))]
    assert slices_of(first, second) == [{"channel": "chat"}, {"channel": "voice"}, {"tag": "x"}]
    assert slice_key({"tag": "x", "channel": "voice"}) == "channel=voice&tag=x"
    assert slice_key({}) == ""


def test_us01_us06_summarize_run_reports_every_metric_with_an_interval() -> None:
    outcomes = [
        _trial(
            f"s{s}",
            passed=(s + i) % 3 != 0,
            channel="voice" if s % 2 else "chat",
            tags=frozenset({"noisy"}) if s == 1 else frozenset(),
            latencies=(400 + 10 * s + i, 900 + s),
        )
        for s in range(6)
        for i in range(4)
    ]
    summaries = summarize_run(outcomes, run_id=RUN, seed=3, resamples=500)
    by_key = _by_key(summaries)
    assert all(s.run_id == RUN for s in summaries)
    assert by_key[("pass_rate", "")].estimate.method is IntervalMethod.CLUSTER_BOOTSTRAP
    assert by_key[("pass_rate", "")].estimate.n == 6
    assert by_key[("pass_rate", "scenario=s0")].estimate.method is IntervalMethod.WILSON
    assert by_key[("pass_rate", "scenario=s0")].estimate.value == pytest.approx(0.5)
    for key in ("channel=chat", "channel=voice", "tag=noisy"):
        assert ("pass_rate", key) in by_key
        assert ("response_latency_p95", key) in by_key
    assert by_key[("pass_rate", "channel=voice")].estimate.n == 3
    for metric in ("pass^3", "pass@3"):
        assert by_key[(metric, "")].estimate.n == 6
    p50 = by_key[("response_latency_p50", "")].estimate
    p99 = by_key[("response_latency_p99", "")].estimate
    assert p50.method is IntervalMethod.BOOTSTRAP
    assert p50.n == 24  # trials, not turns, are the resampling unit
    assert p50.value <= by_key[("response_latency_p95", "")].estimate.value <= p99.value
    for summary in summaries:
        assert summary.estimate.ci_low <= summary.estimate.value <= summary.estimate.ci_high


def test_summarize_run_is_reproducible_and_skips_what_it_cannot_estimate() -> None:
    outcomes = [_trial("a", True), _trial("a", False), _trial("b", True)]
    first = summarize_run(outcomes, run_id=RUN, seed=1, resamples=200, ks=(3,))
    again = summarize_run(outcomes, run_id=RUN, seed=1, resamples=200, ks=(3,))
    assert [s.estimate for s in first] == [s.estimate for s in again]
    metrics = {s.metric for s in first}
    assert "pass^3" not in metrics  # no scenario has 3 trials
    assert not any(m.startswith("response_latency") for m in metrics)  # no latencies recorded
    with pytest.raises(ValueError, match="no outcomes"):
        summarize_run([], run_id=RUN, seed=1, resamples=10)


def test_summarize_run_slice_seeds_are_independent_of_other_slices() -> None:
    base = [_trial(f"s{i}", i % 2 == 0, channel="chat") for i in range(8)]
    extra = [_trial("v", True, channel="voice")]
    alone = _by_key(summarize_run(base, run_id=RUN, seed=4, resamples=300))
    with_more = _by_key(summarize_run(base + extra, run_id=RUN, seed=4, resamples=300))
    assert alone[("pass_rate", "channel=chat")] == with_more[("pass_rate", "channel=chat")]
