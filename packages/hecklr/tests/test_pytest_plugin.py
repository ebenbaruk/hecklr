"""``pytest -p hecklr --hecklr-suite=ci``: one test per scenario, by the documented pass rule."""

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

import hecklr
import hecklr.api
import hecklr.compose
import hecklr.pytest_plugin
from hecklr.testing import scripted_models
from hecklr_core import RunResults

pytest_plugins = ["pytester"]

# pytest imports the plugin (an entry point) before coverage starts; re-executing the module here
# lets coverage see its definitions. The hooks pytest registered keep working either way.
plugin = importlib.reload(hecklr.pytest_plugin)
importlib.reload(hecklr)
scenario_failure = plugin.scenario_failure

# A conftest in the project pins scripted models, as a team's own CI smoke test might.
SCRIPTED_CONFTEST = """
import hecklr.api
from hecklr.testing import scripted_models

_run = hecklr.api.run


def _scripted(*args, **kwargs):
    judge = lambda request: "Saturdays" not in request.messages[-1].content
    return _run(*args, llm_providers=scripted_models(judge), **kwargs)


hecklr.api.run = _scripted
"""


def _pytest(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- runs this interpreter's pytest on a test project
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "hecklr", *args],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


@pytest.mark.parametrize(
    ("passes", "trials", "threshold", "fails"),
    [
        (0, 3, 0.8, True),  # every trial failed
        (1, 3, 0.8, True),  # Wilson upper bound 79% < 80%
        (2, 3, 0.8, False),
        (3, 3, 0.8, False),
        (1, 3, 0.7, False),  # upper bound 79% >= 70%
        (0, 1, 0.1, True),
        (5, 20, 0.8, True),
    ],
)
def test_us01_scenario_pass_rule(passes: int, trials: int, threshold: float, fails: bool) -> None:
    failure = scenario_failure(passes, trials, threshold)

    assert (failure is not None) is fails
    if failure is not None:
        assert f"{passes}/{trials} trials passed" in failure
        assert "95% CI" in failure


def test_scenario_rule_needs_trials() -> None:
    with pytest.raises(ValueError, match="at least one"):
        scenario_failure(0, 0, 0.8)


@pytest.mark.slow
def test_plugin_runs_the_suite_as_one_test_per_scenario(project_dir: Path) -> None:
    (project_dir / "conftest.py").write_text(SCRIPTED_CONFTEST, encoding="utf-8")

    result = _pytest(project_dir, "--hecklr-suite", "ci", "-v", "-rf")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 failed, 4 passed" in result.stdout
    assert "check-appointment PASSED" in result.stdout
    assert "saturday-visits: 0/3 trials passed" in result.stdout
    assert "every trial failed" in result.stdout


def test_plugin_is_inert_without_a_suite(project_dir: Path) -> None:
    (project_dir / "test_plain.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")

    result = _pytest(project_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "--hecklr-suite" in _pytest(project_dir, "--help").stdout


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_plugin_items_in_process(
    project_dir: Path, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real_run = hecklr.api.run

    def scripted(config: Path, *, suite: str) -> RunResults:
        calls.append(suite)
        return real_run(config, suite=suite, trials=1, llm_providers=scripted_models())

    monkeypatch.setattr(hecklr.api, "run", scripted)
    monkeypatch.chdir(project_dir)

    result = pytester.runpytest_inprocess(
        "--hecklr-suite", "ci", "--hecklr-min-pass-rate", "0.5", "-k", "not plan",
        "--rootdir", str(project_dir), str(project_dir),
    )  # fmt: skip

    result.assert_outcomes(passed=4, deselected=1)
    assert calls == ["ci"]  # one run shared by every scenario


def test_scenario_with_no_counted_trial_fails(
    project_dir: Path, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hecklr.api, "run", lambda config, *, suite: _no_trials(config, suite))
    monkeypatch.chdir(project_dir)

    result = pytester.runpytest_inprocess(
        "--hecklr-suite", "ci", "--rootdir", str(project_dir), str(project_dir)
    )

    result.assert_outcomes(failed=5)
    result.stdout.fnmatch_lines(["*no trial could be run and graded*"])


def _no_trials(config: Path, suite: str) -> RunResults:
    plan = hecklr.compose.prepare_run(hecklr.compose.RunRequest(config_path=config, suite=suite))
    return RunResults(run=plan.plan.run, trials=[], events=[], grades=[])


def test_unknown_attribute_of_the_package() -> None:
    with pytest.raises(AttributeError, match="no attribute 'nope'"):
        hecklr.nope  # noqa: B018 -- the attribute access is the test
