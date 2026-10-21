"""SC1: ``hecklr init`` to a first stored, graded run, through the real CLI, in well under 5 min.

Everything is real except the models: the world sandbox server, the in-process starter agent
calling its tools over HTTP, the chat driver, the simulator, the graders, DuckDB storage and the
statistics. The simulator and judge are ``hecklr.testing.scripted_models`` (no network, no cost).
"""

import re
import sys
import time
from pathlib import Path

import anyio
import pytest
from typer.testing import CliRunner

from hecklr import exit_codes
from hecklr.cli import app
from hecklr.commands._common import CliOverrides
from hecklr.scaffold import STARTER_AGENT_MODULE
from hecklr.testing import scripted_models
from hecklr_core import ResultStore

SC1_LIMIT_S = 300


@pytest.mark.e2e
@pytest.mark.fake
@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_sc1_init_then_run_stores_a_graded_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, STARTER_AGENT_MODULE, raising=False)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    started = time.monotonic()

    init = runner.invoke(
        app, ["init", "--yes", "--simulator-model", "fake/customer", "--judge-model", "fake/judge"]
    )
    run = runner.invoke(app, ["run"], obj=CliOverrides(scripted_models()))

    elapsed = time.monotonic() - started
    assert init.exit_code == exit_codes.OK, init.output
    assert run.exit_code == exit_codes.OK, run.output
    assert elapsed < SC1_LIMIT_S
    assert "Suite pass rate" in run.output
    assert "Scenarios (Wilson 95% CI)" in run.output
    match = re.search(r"Run ([0-9A-Z]{26}) \(completed\)", run.output)
    assert match, run.output

    store = ResultStore(tmp_path / ".hecklr" / "runs")
    results = anyio.run(store.load_run, match.group(1))
    store.close()
    assert len(results.trials) == 15  # 5 scenarios x 1 persona x chat x 3 trials
    assert {trial.status for trial in results.trials} == {"ok"}
    graded = {grade.trial_id for grade in results.grades if grade.criterion_index is not None}
    assert graded == {trial.id for trial in results.trials}
    assert any(grade.grader.startswith("judge:") for grade in results.grades)
    assert any(event.kind == "tool_call" for event in results.events)
