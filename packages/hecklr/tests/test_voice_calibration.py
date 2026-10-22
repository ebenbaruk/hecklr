"""The SC4 loopback calibration in process: every metric and injected value is measured."""

import pytest

from hecklr.voice_calibration import (
    INJECTED_DELAYS_MS,
    INJECTED_REACTIONS_MS,
    TimingCheck,
    all_passed,
    run_voice_loopback,
)
from hecklr_schemas import IntervalMethod


@pytest.mark.anyio
@pytest.mark.slow
@pytest.mark.sc4_timing
async def test_sc4_loopback_measures_every_setting() -> None:
    checks = await run_voice_loopback(seed=7)

    assert [(c.metric, c.injected_ms) for c in checks] == [
        *(("response_latency", delay) for delay in INJECTED_DELAYS_MS),
        *(("barge_in_reaction", reaction) for reaction in INJECTED_REACTIONS_MS),
    ]
    assert all(len(check.errors_ms) >= 2 for check in checks)
    assert all_passed(checks), [(c.metric, c.injected_ms, c.worst_ms) for c in checks]


def test_timing_check_verdict_and_interval() -> None:
    check = TimingCheck("response_latency", 300, (2.0, -1.0, 3.0))
    estimate = check.mean_error(seed=0)
    assert (check.passed, check.worst_ms) == (True, 3.0)
    assert estimate.ci_low <= estimate.value <= estimate.ci_high
    assert (estimate.n, estimate.method) == (3, IntervalMethod.BOOTSTRAP)
    assert not TimingCheck("barge_in_reaction", 150, ()).passed
    assert not all_passed([check, TimingCheck("barge_in_reaction", 150, (21.0,))])
    assert not all_passed([])
