"""The SC8 fault matrix on the chat reference agent: clean, A/A and F1-F6, end to end (M1).

For one tier, the script starts the ``acme-maintenance`` agent over HTTP once per variant, runs
``hecklr run`` against it, then gates every candidate against the clean baseline with
``hecklr gate`` and writes a verdict table (``matrix.json`` and ``matrix.md``)::

    uv run python scripts/fault-matrix/fault_matrix.py --tier free
    uv run --env-file .env python scripts/fault-matrix/fault_matrix.py --tier a
    uv run --env-file .env python scripts/fault-matrix/fault_matrix.py --tier b --budget-usd 6

Everything (a copy of the suite files, the result store, reports, logs) goes to ``--out``,
``.hecklr/fault-matrix/<tier>`` (git-ignored) by default.

Tiers:

- ``free``: rules brain, the scripted ``smoke`` suite, deterministic graders only. No model call
  and no key: what CI runs every night.
- ``a``: rules brain, the ``matrix`` suite (smoke plus judge-only twins), real judges: the free,
  deterministic agent graded by every grader the spec names, judges included.
- ``b``: the LLM brain on a real model, guided LLM customers and real judges on the
  ``matrix-live`` suite: the real reference agent.

Each tier runs the clean agent twice (baseline, then an A/A candidate with another seed) and each
fault variant once. The matrix passes when the A/A comparison is not a regression and every fault
is a regression both on the suite pass rate (the gate exits 1) and on the pass rate of each grader
the spec says must catch it, compared per grader with the same paired statistics.

Spend: ``--budget-usd`` is a hard cap on everything the tier spends. Hecklr's own spend (simulator
and judges) is capped through ``hecklr run --budget-usd``; the agent's model spend is capped by
the agent (``--budget-usd`` of ``serve.py``) and read from its ``/healthz``. After the baseline
run, the script projects the whole tier's cost and stops if it would exceed the budget.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Final, Literal, TextIO

import httpx2
import yaml
from pydantic import BaseModel, Field

from hecklr_core import load_project, resolve_suite
from hecklr_report import Analysis, TrialRow
from hecklr_report.outcomes import criterion_label
from hecklr_schemas import Estimate, RunId, Scenario, StatsConfig, SuccessCriterion, Verdict
from hecklr_stats import TrialOutcome, compare_runs

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
PROJECT: Final = REPO_ROOT / "examples" / "acme-maintenance"
SERVE: Final = PROJECT / "agent" / "serve.py"
FAULTS: Final = ("F1", "F2", "F3", "F4", "F5", "F6")
BASELINE: Final = "clean"
AA: Final = "clean-aa"
GATE_REGRESSION: Final = 1
GATE_HARNESS_ERROR: Final = 3
NO_KEY_PLACEHOLDER: Final = "fault-matrix-free-tier-calls-no-model"
"""Set as ``OPENAI_API_KEY`` for the free tier only when none is set: ``hecklr run`` builds the
configured providers even for a suite that calls no model (issue #19). A placeholder can never
spend money: any accidental call fails authentication."""

OUTAGE_ERRORS: Final = frozenset({"LLMProviderError", "AgentBudgetError"})
"""Agent turn failures that are the model provider's or the budget's, never the agent's: a run
with any of them measures an outage, and its agent errors would pass for a caught fault."""

TierName = Literal["free", "a", "b"]

TOOL_GRADERS: Final = frozenset({"tool_called", "tool_not_called", "args_match", "call_order"})
"""The spec's Tool grader family (spec, "Graders"): the fault table's ``tool_called`` for F1."""

SPEC_GRADERS: Final[Mapping[str, tuple[str, ...]]] = {
    "F1": ("tool", "judge:resolved"),
    "F2": ("state",),
    "F3": ("judge:no_hallucination",),
    "F4": ("no_cross_customer_data",),
    "F5": ("no_unauthorized_refund",),
    "F6": ("judge:handoff_appropriate",),
}
"""The graders that must catch each fault (spec, "Fault-injection matrix (SC8)")."""

DETERMINISTIC_GRADERS: Final[Mapping[str, tuple[str, ...]]] = {
    **SPEC_GRADERS,
    "F1": ("tool",),
    "F3": ("not_contains",),
    "F6": ("tool",),
}
"""The free tier's graders: the spec's where they are deterministic, else the deterministic
backstop the scenarios carry (F1 keeps its tool grader without its ``resolved`` judge)."""


@dataclass(frozen=True, slots=True)
class Tier:
    """What one tier runs."""

    brain: Literal["rules", "llm"]
    suite: str
    budget_usd: Decimal
    hecklr_share: Decimal
    """Share of the budget for Hecklr's own calls; the rest is the agent's."""
    expected: Mapping[str, tuple[str, ...]]
    calls_models: bool
    """Whether Hecklr calls a model (judges, guided customers); the free tier must not."""


# Tier b's Hecklr share: in the M1 pilots (luna everywhere) the agent's own calls were 23-27% of
# spend; 30% leaves the agent's pool a margin so it never runs dry mid-run.
TIERS: Final[Mapping[TierName, Tier]] = {
    "free": Tier("rules", "smoke", Decimal("0.10"), Decimal(1), DETERMINISTIC_GRADERS, False),
    "a": Tier("rules", "matrix", Decimal("0.50"), Decimal(1), SPEC_GRADERS, True),
    "b": Tier("llm", "matrix-live", Decimal(6), Decimal("0.7"), SPEC_GRADERS, True),
}


class MatrixError(Exception):
    """The matrix could not run: an agent that never came up, a crashed run, the budget."""


# --- results -------------------------------------------------------------------------------------


class GraderResult(BaseModel):
    """One grader's pass rate in the baseline and the candidate, compared like the suite."""

    grader: str
    """``judge:<rubric>`` or the criterion type, e.g. ``state``, ``tool_called``."""
    n_scenarios: int
    baseline: Estimate
    candidate: Estimate
    delta: Estimate
    p_value: float
    verdict: Verdict


class Row(BaseModel):
    """One candidate run gated against the clean baseline."""

    variant: str
    run_id: str
    expected: list[str]
    """Graders that must flag it; empty for the A/A row, which must flag nothing."""
    gate_exit: int
    verdict: Verdict | None
    delta: Estimate | None
    p_value: float | None
    graders: list[GraderResult]
    caught: bool
    """Fault rows: a suite regression flagged by every expected grader. A/A: no regression."""
    hecklr_spent_usd: Decimal
    agent_spent_usd: Decimal
    report_dir: str

    @property
    def flagged(self) -> list[str]:
        """Graders whose own comparison is a regression."""
        return [g.grader for g in self.graders if g.verdict is Verdict.REGRESSION]


class Matrix(BaseModel):
    """A whole tier: the baseline, every candidate row and what it cost."""

    schema_version: int = 1
    created_at: datetime
    tier: TierName
    suite: str
    brain: str
    simulator_model: str | None
    judge_model: str | None
    agent_model: str | None
    seed: int
    baseline_run_id: str
    baseline_hecklr_spent_usd: Decimal
    baseline_agent_spent_usd: Decimal
    rows: list[Row] = Field(default_factory=list)
    budget_usd: Decimal
    spent_usd: Decimal
    passed: bool


# --- grader attribution --------------------------------------------------------------------------


def grader_family(criterion: SuccessCriterion) -> str:
    """The grader a criterion belongs to, as the spec's fault table names graders.

    The four tool criteria are one family, ``tool``: F1's tool-level symptom on a strong model is
    a booking call with a slot the customer never chose, corrected after they object, which only
    ``args_match`` sees (``tool_called`` finds the corrected call). ADR 0020a.
    """
    if criterion.type == "judge":
        return f"judge:{criterion.rubric}"
    if criterion.type in TOOL_GRADERS:
        return "tool"
    return criterion.type


def families_by_label(scenarios: Mapping[str, Scenario]) -> dict[str, dict[str, str]]:
    """Per scenario, the grader family of each failure-cause label its criteria produce."""
    return {
        sid: {criterion_label(c): grader_family(c) for c in scenario.success}
        for sid, scenario in scenarios.items()
    }


def grader_outcomes(
    rows: Sequence[TrialRow], family: str, families: Mapping[str, Mapping[str, str]]
) -> list[TrialOutcome]:
    """Each analyzable trial of a scenario that carries ``family``: did that grader pass?

    A trial that did not end normally (agent error, timeout) fails every grader, as it fails
    the trial (``hecklr_report.outcomes``).
    """
    outcomes = []
    for row in rows:
        labels = families.get(row.scenario_id, {})
        if row.passed is None or family not in labels.values():
            continue
        failed = row.status != "ok" or any(
            labels.get(cause.cause) == family for cause in row.causes
        )
        outcomes.append(TrialOutcome(scenario_id=row.scenario_id, passed=not failed))
    return outcomes


def compare_graders(
    baseline: Analysis,
    candidate: Analysis,
    scenarios: Mapping[str, Scenario],
    stats: StatsConfig,
) -> list[GraderResult]:
    """Every grader family's pass rate, candidate vs baseline, paired by scenario."""
    return compare_grader_rows(
        baseline.trials,
        candidate.trials,
        scenarios,
        stats,
        runs=(baseline.candidate.id, candidate.candidate.id),
        seed=candidate.seed,
    )


def compare_grader_rows(
    baseline: Sequence[TrialRow],
    candidate: Sequence[TrialRow],
    scenarios: Mapping[str, Scenario],
    stats: StatsConfig,
    *,
    runs: tuple[RunId, RunId],
    seed: int,
) -> list[GraderResult]:
    """``compare_graders`` on the two runs' trial rows; ``runs`` is (baseline, candidate)."""
    families = families_by_label(scenarios)
    names = sorted({family for labels in families.values() for family in labels.values()})
    results = []
    for family in names:
        base = grader_outcomes(baseline, family, families)
        cand = grader_outcomes(candidate, family, families)
        if not {o.scenario_id for o in base} & {o.scenario_id for o in cand}:
            continue
        suite = compare_runs(
            base, cand, baseline_run=runs[0], candidate_run=runs[1], config=stats, seed=seed
        )[0]
        results.append(
            GraderResult(
                grader=family,
                n_scenarios=suite.n_scenarios,
                baseline=suite.baseline_estimate,
                candidate=suite.candidate_estimate,
                delta=suite.delta,
                p_value=suite.p_value,
                verdict=suite.verdict,
            )
        )
    return results


def is_caught(expected: Sequence[str], gate_exit: int, graders: Sequence[GraderResult]) -> bool:
    """Whether a row meets SC8: faults regress on every expected grader, A/A on nothing.

    A fault must fail the gate as a regression (exit 1) and regress on each grader the spec
    names. The A/A row (no expected grader) must not fail the gate as a regression or a harness
    error; an inconclusive gate is not a false alarm.
    """
    if not expected:
        return gate_exit not in (GATE_REGRESSION, GATE_HARNESS_ERROR)
    flagged = {g.grader for g in graders if g.verdict is Verdict.REGRESSION}
    return gate_exit == GATE_REGRESSION and set(expected) <= flagged


# --- rendering -----------------------------------------------------------------------------------


def points(estimate: Estimate) -> str:
    """A pass-rate difference in percentage points with its interval."""
    return (
        f"{estimate.value * 100:+.1f} pts "
        f"[{estimate.ci_low * 100:+.1f}, {estimate.ci_high * 100:+.1f}]"
    )


def _grader_cell(row: Row) -> str:
    shown = []
    for grader in row.graders:
        expected = grader.grader in row.expected
        if expected or grader.verdict is Verdict.REGRESSION:
            name = f"**{grader.grader}**" if expected else grader.grader
            shown.append(
                f"{name} {points(grader.delta)} p={grader.p_value:.3f} {grader.verdict.value}"
            )
    return "<br>".join(shown) or "none"


def render_markdown(matrix: Matrix) -> str:
    """The verdict table: one row per candidate, every number with its interval."""
    lines = [
        f"### Fault matrix, tier {matrix.tier}: {'PASS' if matrix.passed else 'FAIL'}",
        "",
        f"Suite `{matrix.suite}`, {matrix.brain} brain"
        + (f" ({matrix.agent_model})" if matrix.agent_model else "")
        + f"; simulator {matrix.simulator_model or 'scripted'}, judge "
        f"{matrix.judge_model or 'none'}; seed {matrix.seed}; baseline run "
        f"`{matrix.baseline_run_id}`. Spent ${matrix.spent_usd:.4f} of ${matrix.budget_usd}.",
        "",
        "| Variant | Must be caught by | Verdict | Δ pass rate [95% CI] | p | Gate exit "
        "| Graders (Δ [CI], p) | Result |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in matrix.rows:
        result = "caught" if row.caught else "MISSED"
        if not row.expected:
            result = "no false alarm" if row.caught else "FALSE ALARM"
        lines.append(
            f"| {row.variant} | {', '.join(row.expected) or '(nothing: A/A)'} "
            f"| {row.verdict.value if row.verdict else 'none'} "
            f"| {points(row.delta) if row.delta else 'n/a'} "
            f"| {f'{row.p_value:.3f}' if row.p_value is not None else 'n/a'} "
            f"| {row.gate_exit} | {_grader_cell(row)} | {result} |"
        )
    lines += [
        "",
        "Expected graders in bold; other graders are listed when they regressed too. Δ is the "
        "candidate's pass rate minus the baseline's, paired by scenario (paired cluster "
        "bootstrap interval, sign-flip permutation p).",
    ]
    return "\n".join(lines) + "\n"


# --- processes -----------------------------------------------------------------------------------


def free_port() -> int:
    """An ephemeral localhost port that was free a moment ago."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@dataclass(frozen=True, slots=True)
class Health:
    """What the agent's ``/healthz`` said after a run."""

    variant: str
    spent_usd: Decimal
    requests_by_world_source: dict[str, int]
    failed_turns: dict[str, int]


@contextmanager
def agent_server(
    *,
    brain: str,
    fault: str | None,
    port: int,
    seed: int,
    cap_usd: Decimal | None,
    log: TextIO,
    startup_s: float = 60.0,
) -> Iterator[str]:
    """Run the reference agent on ``port`` until the block ends; yields its base URL."""
    command = [sys.executable, str(SERVE), "--brain", brain, "--port", str(port)]
    if fault is not None:
        command += ["--fault", fault]
    if cap_usd is not None:
        command += ["--budget-usd", str(cap_usd)]
    env = {**os.environ, "ACME_SEED": str(seed)}
    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env)  # noqa: S603 -- our own interpreter and script, no shell
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_until_healthy(process, url, startup_s)
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _wait_until_healthy(process: subprocess.Popen[bytes], url: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise MatrixError(f"the agent exited with code {process.returncode} before serving")
        try:
            if httpx2.get(f"{url}/healthz", timeout=2).status_code == 200:
                return
        except httpx2.TransportError:
            pass  # not listening yet
        time.sleep(0.2)
    raise MatrixError(f"the agent did not answer {url}/healthz within {timeout_s:.0f} s")


def read_health(url: str) -> Health:
    """The agent's ``/healthz`` as a ``Health``."""
    body = httpx2.get(f"{url}/healthz", timeout=5).raise_for_status().json()
    return Health(
        variant=body["variant"],
        spent_usd=Decimal(str(body.get("spent_usd", "0"))),
        requests_by_world_source=dict(body["requests_by_world_source"]),
        failed_turns=dict(body.get("failed_turns", {})),
    )


def check_health(health: Health, variant: str) -> None:
    """Refuse a run whose agent did not measure ``variant`` cleanly.

    Raises:
        MatrixError: the agent ran another variant, served tools outside the trial sandboxes,
            or had turns fail because of its model provider or its budget.
    """
    expected = BASELINE if variant in (BASELINE, AA) else variant
    if health.variant != expected:
        raise MatrixError(f"the agent ran {health.variant!r}, not {variant!r}")
    outside = {s: n for s, n in health.requests_by_world_source.items() if s != "header" and n}
    if outside:
        # Tools that did not act on the trial's sandbox are invisible to the graders.
        raise MatrixError(f"the agent served requests outside the trial sandboxes: {outside}")
    outage = {e: n for e, n in health.failed_turns.items() if e in OUTAGE_ERRORS and n}
    if outage:
        raise MatrixError(
            f"the agent's model calls failed during {variant} ({outage}): the run measured "
            "an outage or an exhausted quota, not the variant; see its agent log"
        )


def hecklr(args: Sequence[str], *, log: TextIO, env: Mapping[str, str]) -> tuple[int, str]:
    """Run the ``hecklr`` CLI of this interpreter; returns its exit code and stdout."""
    executable = Path(sys.executable).with_name("hecklr")
    done = subprocess.run(  # noqa: S603 -- the workspace's own CLI, no shell
        [str(executable), *args], capture_output=True, text=True, env=dict(env), check=False
    )
    log.write(f"$ hecklr {' '.join(args)}\n{done.stdout}{done.stderr}\n[exit {done.returncode}]\n")
    log.flush()
    return done.returncode, done.stdout


_RUN_LINE: Final = re.compile(r"^Run (\S+) \((\w+)\)", re.M)
_ANSI_ESCAPE: Final = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def recorded_run(stdout: str) -> str:
    """The run id ``hecklr run`` printed (styled when ``FORCE_COLOR`` is set, as in CI)."""
    match = _RUN_LINE.search(_ANSI_ESCAPE.sub("", stdout))
    if match is None:
        raise MatrixError("hecklr run printed no run id; see the run log")
    return match.group(1)


# --- the project copy ----------------------------------------------------------------------------


def derive_config(
    config: Mapping[str, object], *, port: int, store: Path, concurrency: int | None
) -> dict[str, object]:
    """``hecklr.yaml`` pointed at the agent on ``port`` and a private result store.

    ``concurrency`` replaces ``budget.concurrency`` when given (a real model is slow per turn,
    and a laptop run of a few hundred trials at 4 at a time takes hours).
    """
    derived = json.loads(json.dumps(config))
    chat = derived["agent"]["adapters"]["chat"]
    url, replaced = re.subn(r"^http://127\.0\.0\.1:\d+/", f"http://127.0.0.1:{port}/", chat["url"])
    if not replaced:
        # Without the rewrite every variant would talk to whatever listens on the original port.
        raise MatrixError(
            f"cannot point {chat['url']!r} at the agent: expected http://127.0.0.1:<port>/"
        )
    chat["url"] = url
    derived["output_dir"] = str(store)
    if concurrency is not None:
        derived.setdefault("budget", {})["concurrency"] = concurrency
    return dict(derived)


def prepare_project(out: Path, port: int, concurrency: int | None) -> Path:
    """Copy the reference project's suite files into ``out`` with a derived ``hecklr.yaml``.

    The copy is what was tested (scenarios, personas, world) and keeps every run's results out of
    the repository. The agent itself always runs from the repository.
    """
    target = out / "project"
    for name in ("scenarios", "scripted", "personas", "worlds", "prompts", "tools"):
        _copy_tree(PROJECT / name, target / name)
    source = yaml.safe_load((PROJECT / "hecklr.yaml").read_text(encoding="utf-8"))
    derived = derive_config(source, port=port, store=out / "store", concurrency=concurrency)
    (target / "hecklr.yaml").write_text(yaml.safe_dump(derived, sort_keys=False), "utf-8")
    return target / "hecklr.yaml"


def _copy_tree(source: Path, target: Path) -> None:
    for path in source.rglob("*"):
        if path.is_file():
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(path.read_bytes())


# --- the matrix ----------------------------------------------------------------------------------


@dataclass
class Budget:
    """Two pools under one hard cap: Hecklr's own calls and the agent's."""

    hecklr_usd: Decimal
    agent_usd: Decimal
    hecklr_spent: Decimal = Decimal(0)
    agent_spent: Decimal = Decimal(0)

    @classmethod
    def split(cls, total: Decimal, hecklr_share: Decimal) -> "Budget":
        """``total`` split between Hecklr (``hecklr_share``) and the agent."""
        hecklr_usd = total * hecklr_share
        return cls(hecklr_usd=hecklr_usd, agent_usd=total - hecklr_usd)

    @property
    def total(self) -> Decimal:
        """The hard cap: both pools."""
        return self.hecklr_usd + self.agent_usd

    @property
    def spent(self) -> Decimal:
        """Everything spent so far."""
        return self.hecklr_spent + self.agent_spent

    def hecklr_left(self) -> Decimal:
        """Hecklr's remaining pool, rounded down to a cent's hundredth."""
        return max(self.hecklr_usd - self.hecklr_spent, Decimal(0)).quantize(
            Decimal("0.0001"), ROUND_DOWN
        )

    def agent_left(self) -> Decimal:
        """The agent's remaining pool, rounded down to a cent's hundredth."""
        return max(self.agent_usd - self.agent_spent, Decimal(0)).quantize(
            Decimal("0.0001"), ROUND_DOWN
        )


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One ``hecklr run`` of one variant."""

    variant: str
    run_id: str
    hecklr_spent_usd: Decimal
    agent_spent_usd: Decimal


@dataclass
class Runner:
    """Runs the tier's variants one after the other, then gates them."""

    tier_name: TierName
    tier: Tier
    out: Path
    config: Path
    port: int
    seed: int
    budget: Budget
    trials: int | None
    env: dict[str, str]
    echo: TextIO

    def run_variant(self, variant: str, index: int) -> RunRecord:
        """Start the agent for ``variant``, run the suite against it and stop the agent."""
        fault = None if variant in (BASELINE, AA) else variant
        seed = self.seed + index
        logs = self.out / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        if self.budget.hecklr_left() <= 0 and self.tier.calls_models:
            raise MatrixError(f"the budget is spent (${self.budget.spent:.4f}); stopping")
        agent_cap = self.budget.agent_left() if self.tier.brain == "llm" else None
        args = ["run", "-c", str(self.config), "--suite", self.tier.suite, "--seed", str(seed)]
        args += ["--budget-usd", str(self.budget.hecklr_left())]
        if self.trials is not None:
            args += ["--trials", str(self.trials)]
        with (
            (logs / f"{variant}-agent.log").open("w", encoding="utf-8") as agent_log,
            (logs / f"{variant}-run.log").open("w", encoding="utf-8") as run_log,
            agent_server(
                brain=self.tier.brain,
                fault=fault,
                port=self.port,
                seed=seed,
                cap_usd=agent_cap,
                log=agent_log,
            ) as url,
        ):
            code, stdout = hecklr(args, log=run_log, env=self.env)
            health = read_health(url)
        if code != 0:
            raise MatrixError(f"hecklr run for {variant} exited {code}; see {run_log.name}")
        check_health(health, variant)
        run_id = recorded_run(stdout)
        spent = self._hecklr_spend(run_id)
        if not self.tier.calls_models and spent + health.spent_usd > 0:
            raise MatrixError(f"tier {self.tier_name} must not spend, but run {run_id} did")
        self.budget.hecklr_spent += spent
        self.budget.agent_spent += health.spent_usd
        record = RunRecord(variant, run_id, spent, health.spent_usd)
        self.echo.write(
            f"{variant}: run {run_id}, hecklr ${spent:.4f} + agent ${health.spent_usd:.4f} "
            f"(total so far ${self.budget.spent:.4f})\n"
        )
        self.echo.flush()  # a tier runs for an hour; show progress when stdout is a file
        return record

    def _hecklr_spend(self, run_id: str) -> Decimal:
        run = json.loads((self.out / "store" / "runs" / run_id / "run.json").read_text("utf-8"))
        return Decimal(str(run["spent_usd"]))

    def report(self, record: RunRecord) -> Analysis:
        """The baseline's own analysis (its trials and failure causes) and its HTML report."""
        target = self.out / "reports" / record.variant
        args = ["report", record.run_id, "-c", str(self.config), "-f", "json", "-f", "html"]
        with (self.out / "logs" / f"{record.variant}-report.log").open(
            "w", encoding="utf-8"
        ) as log:
            code, _ = hecklr([*args, "--out-dir", str(target)], log=log, env=self.env)
        if code != 0:
            raise MatrixError(f"hecklr report for {record.variant} exited {code}")
        return Analysis.model_validate_json((target / "report.json").read_text("utf-8"))

    def gate(self, record: RunRecord, baseline: RunRecord) -> tuple[int, Analysis]:
        """``hecklr gate`` of ``record`` against ``baseline``: exit code and analysis."""
        target = self.out / "reports" / record.variant
        args = ["gate", "-c", str(self.config), "--candidate", record.run_id]
        args += ["--baseline", baseline.run_id, "--out-dir", str(target)]
        with (self.out / "logs" / f"{record.variant}-gate.log").open("w", encoding="utf-8") as log:
            code, _ = hecklr(args, log=log, env=self.env)
        report = target / "report.json"
        if not report.is_file():
            raise MatrixError(f"hecklr gate for {record.variant} exited {code} without a report")
        return code, Analysis.model_validate_json(report.read_text("utf-8"))


def run_matrix(runner: Runner, variants: Sequence[str]) -> Matrix:
    """Baseline, then every variant; gate each against the baseline and build the table.

    The baseline doubles as the cost pilot: if every run costs what it did, the tier must fit
    the budget, or nothing more is spent.
    """
    baseline = runner.run_variant(BASELINE, 0)
    projected = (baseline.hecklr_spent_usd + baseline.agent_spent_usd) * (1 + len(variants))
    runner.echo.write(
        f"Projected cost of {1 + len(variants)} runs: ${projected:.4f} "
        f"(budget ${runner.budget.total:.2f})\n"
    )
    if projected > runner.budget.total:
        raise MatrixError(f"projected cost ${projected:.4f} exceeds the budget; stopping early")
    records = [runner.run_variant(v, i) for i, v in enumerate(variants, start=1)]
    project = load_project(runner.config)
    scenarios = resolve_suite(project, runner.tier.suite).scenarios
    base_analysis = runner.report(baseline)
    rows = []
    for record in records:
        code, analysis = runner.gate(record, baseline)
        graders = compare_graders(base_analysis, analysis, scenarios, project.config.stats)
        expected = list(runner.tier.expected.get(record.variant, ()))
        comparison = analysis.suite_comparison
        rows.append(
            Row(
                variant=record.variant,
                run_id=record.run_id,
                expected=expected,
                gate_exit=code,
                verdict=analysis.verdict,
                delta=comparison.delta if comparison else None,
                p_value=comparison.p_value if comparison else None,
                graders=graders,
                caught=is_caught(expected, code, graders),
                hecklr_spent_usd=record.hecklr_spent_usd,
                agent_spent_usd=record.agent_spent_usd,
                report_dir=str(runner.out / "reports" / record.variant),
            )
        )
    info = base_analysis.candidate
    return Matrix(
        created_at=datetime.now(UTC),
        tier=runner.tier_name,
        suite=runner.tier.suite,
        brain=runner.tier.brain,
        simulator_model=info.simulator_model if runner.tier.brain == "llm" else None,
        judge_model=info.judge_model if runner.tier.calls_models else None,
        agent_model=os.environ.get("ACME_MODEL", "openai/gpt-6-luna")
        if runner.tier.brain == "llm"
        else None,
        seed=runner.seed,
        baseline_run_id=baseline.run_id,
        baseline_hecklr_spent_usd=baseline.hecklr_spent_usd,
        baseline_agent_spent_usd=baseline.agent_spent_usd,
        rows=rows,
        budget_usd=runner.budget.total,
        spent_usd=runner.budget.spent,
        passed=bool(rows) and all(row.caught for row in rows),
    )


# --- command line --------------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """The command line flags."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tier", choices=list(TIERS), required=True)
    parser.add_argument(
        "--out", type=Path, help="output directory [default: .hecklr/fault-matrix/<tier>]"
    )
    parser.add_argument("--budget-usd", type=Decimal, help="hard cap on all spend [tier default]")
    parser.add_argument("--trials", type=int, help="trials per scenario [the suite's]")
    parser.add_argument("--seed", type=int, help="root seed [hecklr.yaml's]")
    parser.add_argument("--concurrency", type=int, help="trials at once [hecklr.yaml's]")
    parser.add_argument(
        "--variants",
        default=",".join((AA, *FAULTS)),
        help=f"comma-separated candidates to gate against the baseline [{AA},F1..F6]",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one tier; exit 0 when every row meets SC8, 1 when one does not, 3 when it broke."""
    args = parse_args(argv)
    tier_name: TierName = args.tier
    tier = TIERS[tier_name]
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    unknown = sorted(set(variants) - {AA, *FAULTS})
    if unknown:
        sys.stderr.write(f"unknown variants: {unknown}\n")
        return 2
    out: Path = (args.out or REPO_ROOT / ".hecklr" / "fault-matrix" / tier_name).resolve()
    out.mkdir(parents=True, exist_ok=True)
    port = free_port()
    env = dict(os.environ)
    if not tier.calls_models:
        env.setdefault("OPENAI_API_KEY", NO_KEY_PLACEHOLDER)
    budget = Budget.split(args.budget_usd or tier.budget_usd, tier.hecklr_share)
    try:
        config = prepare_project(out, port, args.concurrency)
        seed = (
            args.seed if args.seed is not None else int(yaml.safe_load(config.read_text())["seed"])
        )
        runner = Runner(
            tier_name, tier, out, config, port, seed, budget, args.trials, env, sys.stdout
        )
        matrix = run_matrix(runner, variants)
    except MatrixError as exc:
        sys.stderr.write(f"fault matrix: {exc}\n")
        return 3
    (out / "matrix.json").write_text(matrix.model_dump_json(indent=2), "utf-8")
    markdown = render_markdown(matrix)
    (out / "matrix.md").write_text(markdown, "utf-8")
    sys.stdout.write(markdown)
    return 0 if matrix.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
