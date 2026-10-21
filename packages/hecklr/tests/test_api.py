"""``hecklr.run`` and ``hecklr.run_async``: the programmatic twin of ``hecklr run``."""

from decimal import Decimal
from pathlib import Path

import pytest

import hecklr
from hecklr.testing import scripted_models
from hecklr_schemas import Channel, RunStatus

# The world sandbox and the agents under test listen on 127.0.0.1 ephemeral ports.
pytestmark = [pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]


def test_us01_blocking_run_returns_stored_results(config_path: Path, tmp_path: Path) -> None:
    results = hecklr.run(
        str(config_path),
        suite="ci",
        channels=["chat"],
        trials=1,
        budget_usd=0.5,
        seed=11,
        output_dir=str(tmp_path / "out"),
        llm_providers=scripted_models(),
    )

    assert results.run.status is RunStatus.COMPLETED
    assert results.run.seed == 11
    assert results.run.budget.usd == Decimal("0.5")
    assert {trial.channel for trial in results.trials} == {Channel.CHAT}
    assert (tmp_path / "out" / "runs" / results.run.id / "run.json").is_file()


@pytest.mark.anyio
async def test_async_run_with_adaptive_and_baseline(config_path: Path) -> None:
    baseline = await hecklr.run_async(
        config_path, suite="ci", trials=1, llm_providers=scripted_models()
    )

    results = await hecklr.run_async(
        config_path,
        suite="ci",
        trials=1,
        adaptive=True,
        baseline=baseline.run.id,
        llm_providers=scripted_models(),
    )

    assert results.run.baseline_id == baseline.run.id
    assert results.run.adaptive
    assert len(results.trials) > len(baseline.trials)
