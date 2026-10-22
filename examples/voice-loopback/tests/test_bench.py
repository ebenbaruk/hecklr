"""The measuring side: error summaries, CPU accounting, and the SC3/SC4 scripts end to end."""

import os
from pathlib import Path

import anyio
import pytest
import sc3_load
import sc4_calibrate
from loopback_agent.bench import (
    LoopLag,
    _clock_seconds,
    child_pids,
    cpu_seconds,
    error_summary,
    json_default,
)

pytestmark = [pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]


def test_error_summary_measures_bias_spread_and_worst_case_per_call() -> None:
    summary = error_summary([[602, 598, 605], [610, None], [600]], 600.0, seed=1)
    assert (summary.count, summary.calls, summary.missing) == (5, 3, 1)
    assert summary.worst_ms == 10.0
    assert summary.bias_ms.value == 2.0
    assert summary.bias_ms.ci_low <= 2.0 <= summary.bias_ms.ci_high
    assert summary.p95_ms.value == 10.0
    assert summary.p95_ms.n == 3  # calls, not turns, are the independent unit
    assert not summary.within(20.0)  # a missing measurement is never within tolerance
    assert error_summary([[605]], 600.0, seed=1).within(5.0)
    assert not error_summary([[605]], 600.0, seed=1).within(4.0)


def test_reports_serialize_estimates_as_fields() -> None:
    summary = error_summary([[602]], 600.0, seed=1)
    dumped = json_default(summary.bias_ms)
    assert isinstance(dumped, dict)
    assert dumped["value"] == 2.0
    assert json_default(Path("x")) == "x"


def test_error_summary_needs_a_measurement() -> None:
    with pytest.raises(ValueError, match="no measurement"):
        error_summary([[None]], 600.0, seed=1)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("0:01.50", 1.5), ("1:02:03.00", 3723.0), ("2-00:00:01", 172_801.0)],
)
def test_ps_cpu_times_parse(text: str, seconds: float) -> None:
    assert _clock_seconds(text) == pytest.approx(seconds)


def test_cpu_of_this_process_is_visible() -> None:
    assert cpu_seconds([os.getpid()]) > 0
    assert cpu_seconds([]) == 0.0
    assert all(isinstance(pid, int) for pid in child_pids())


@pytest.mark.anyio
async def test_loop_lag_beats_until_cancelled() -> None:
    lag = LoopLag(period_ms=1.0)
    with anyio.move_on_after(0.05):
        await lag.run()
    assert lag.beats > 5
    assert lag.worst_ms >= 0.0


@pytest.mark.anyio
@pytest.mark.voice
@pytest.mark.usefixtures("frozen_heap")
@pytest.mark.sc4_timing
async def test_sc4_calibration_case_runs_end_to_end() -> None:
    result = await sc4_calibrate.calibrate_case(300.0, 150.0, calls=1)
    assert result.statuses == ["ok"]
    assert result.within(20.0), sc4_calibrate.describe("latency", result.latency)
    assert "bias" in sc4_calibrate.describe("barge-in", result.reaction)


@pytest.mark.anyio
@pytest.mark.voice
async def test_sc3_load_level_reports_transport_health_and_cpu() -> None:
    result = await sc3_load.load_level(4)
    assert result.statuses == {"ok": 4}
    assert result.latency.count >= 4
    assert result.agent_cores > 0
    assert "SC3" in sc3_load.describe(result)


def test_scripts_write_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_cases(calls: int, tolerance_ms: float) -> list[sc4_calibrate.CaseResult]:
        return []

    async def no_levels(levels: list[int]) -> list[sc3_load.LoadResult]:
        return []

    monkeypatch.setattr(sc4_calibrate, "run", no_cases)
    monkeypatch.setattr(sc3_load, "run", no_levels)
    assert sc4_calibrate.main(["--calls=1", f"--json={tmp_path / 'sc4.json'}"]) == 0
    assert sc3_load.main(["--calls", "1", f"--json={tmp_path / 'sc3.json'}"]) == 0
    assert (tmp_path / "sc4.json").read_text().strip() == "[]"
    assert (tmp_path / "sc3.json").read_text().strip() == "[]"
