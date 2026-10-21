"""pytest plugin: a Hecklr suite as one test per scenario.

::

    pytest -p hecklr --hecklr-suite=ci [--hecklr-config=hecklr.yaml] [--hecklr-min-pass-rate=0.8]

The plugin is installed with Hecklr but stays inert until ``--hecklr-suite`` is given, so it
never touches unrelated test runs. The suite runs once, when its first test runs; every test then
reads its scenario's trials (aggregated over personas and channels).

**Pass rule.** A scenario's test fails when every counted trial failed, or when the upper bound
of the 95% Wilson interval of its pass rate is below ``--hecklr-min-pass-rate``: the trials show,
at 95% confidence, that the scenario passes less often than required. Few trials therefore fail a
scenario only on strong evidence (3 trials at the default 0.8: 0/3 and 1/3 fail, 2/3 passes).
Harness errors and grading errors are not counted; a scenario with no counted trial fails too.
Every scenario fails when the run did not complete (it stopped at the budget, was cancelled or
crashed): a truncated run is never a green build.

The suite runs at most once per session, even when it raises: every scenario then fails with the
same error instead of starting the suite again.

Run the suite in one process: with ``pytest -n``, every worker would run the whole suite.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

if TYPE_CHECKING:
    from hecklr_core import RunResults

DEFAULT_MIN_PASS_RATE: Final = 0.8


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the ``--hecklr-*`` options."""
    group = parser.getgroup("hecklr", "Hecklr suites as tests")
    group.addoption("--hecklr-suite", default=None, help="Suite of hecklr.yaml to run as tests.")
    group.addoption(
        "--hecklr-config", default="hecklr.yaml", help="Path to hecklr.yaml [hecklr.yaml]."
    )
    group.addoption(
        "--hecklr-min-pass-rate",
        type=float,
        default=DEFAULT_MIN_PASS_RATE,
        help=f"Fail a scenario whose pass-rate CI lies below this [{DEFAULT_MIN_PASS_RATE}].",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Add one item per scenario of the suite; first, so ``-k`` and ``-m`` see them."""
    suite = config.getoption("hecklr_suite")
    if suite is None:
        return
    path = Path(config.getoption("hecklr_config")).resolve()
    collector = HecklrSuite.from_parent(session, path=path, suite=suite)
    items.extend(collector.collect())


def scenario_failure(passes: int, trials: int, min_pass_rate: float) -> str | None:
    """Why a scenario with ``passes`` of ``trials`` counted trials fails; ``None`` if it passes."""
    from hecklr_stats import wilson_estimate

    if trials < 1:
        raise ValueError("a scenario needs at least one counted trial")
    rate = wilson_estimate(passes, trials)
    observed = f"{passes}/{trials} trials passed, pass rate {rate.value:.0%} " + (
        f"[95% CI {rate.ci_low:.0%}, {rate.ci_high:.0%}]"
    )
    if passes == 0:
        return f"{observed}: every trial failed"
    if rate.ci_high < min_pass_rate:
        return f"{observed}: the whole interval is below the required {min_pass_rate:.0%}"
    return None


class HecklrSuite(pytest.File):
    """A suite of ``hecklr.yaml``; runs it on first use and shares the results."""

    def __init__(self, *, suite: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]  # pytest passes node arguments through
        self.suite = suite
        self._results: RunResults | None = None
        self._error: str | None = None

    def collect(self) -> Iterator["ScenarioItem"]:
        """One item per scenario the suite selects."""
        from hecklr.compose import load_run_project
        from hecklr_core import resolve_suite

        project = load_run_project(self.path)
        for scenario_id in sorted(resolve_suite(project, self.suite).scenarios):
            yield ScenarioItem.from_parent(self, name=scenario_id, scenario_id=scenario_id)

    def results(self) -> "RunResults":
        """The suite's results, running it the first time they are needed.

        A run that raises is remembered too: re-running a crashing suite once per scenario would
        multiply its wall time and its model spend (each attempt gets a fresh budget).
        """
        if self._error is None and self._results is None:
            from hecklr.api import run
            from hecklr.errors import describe_error

            try:
                self._results = run(self.path, suite=self.suite)
            except Exception as exc:  # noqa: BLE001 -- any failure fails every scenario, below
                self._error = f"the suite run failed: {describe_error(exc)}"
        if self._results is None:
            assert self._error is not None  # noqa: S101 -- one of the two is always set here
            pytest.fail(self._error, pytrace=False)
        return self._results


class ScenarioItem(pytest.Item):
    """One scenario of the suite, judged on all its trials."""

    def __init__(self, *, scenario_id: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]  # pytest passes node arguments through
        self.scenario_id = scenario_id

    def runtest(self) -> None:
        """Fail by the pass rule in the module docstring."""
        from hecklr.outcomes import trial_outcomes
        from hecklr_schemas import RunStatus

        assert isinstance(self.parent, HecklrSuite)  # noqa: S101 -- created only by HecklrSuite
        results = self.parent.results()
        if results.run.status is not RunStatus.COMPLETED:
            pytest.fail(
                f"scenario {self.scenario_id}: the run {results.run.id} ended "
                f"{results.run.status.value}, so its results are partial",
                pytrace=False,
            )
        counted = [
            outcome
            for outcome in trial_outcomes(results).outcomes
            if outcome.scenario_id == self.scenario_id
        ]
        if not counted:
            pytest.fail(
                f"scenario {self.scenario_id}: no trial could be run and graded", pytrace=False
            )
        passes = sum(outcome.passed for outcome in counted)
        threshold = self.config.getoption("hecklr_min_pass_rate")
        failure = scenario_failure(passes, len(counted), threshold)
        if failure is not None:
            pytest.fail(f"scenario {self.scenario_id}: {failure}", pytrace=False)

    def reportinfo(self) -> tuple[Path, int | None, str]:
        """Where the scenario comes from, for pytest's report."""
        return self.path, None, f"hecklr scenario {self.scenario_id}"
