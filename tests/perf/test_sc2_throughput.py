"""SC2: 200 scenarios x 3 trials in under 10 minutes at concurrency 32 (text throughput).

Everything a real ``hecklr run`` does runs here except the models: the plan, the orchestrator
with 32 trials in flight, a world sandbox per trial, the chat driver and HTTP/SSE adapter, the
acme reference agent (rules brain) in its own process calling its tools over HTTP, a simulated
customer and judges per trial, DuckDB storage. Simulator and judges are ``FakeLLM``: a customer
who states the goal, gives every fact at once, agrees twice and leaves, so the agent verifies the
caller and calls its tools; and a judge that passes. SC2 measures Hecklr, not a model provider.

Run serially (the ``perf`` marker is skipped under xdist): ``uv run pytest -m perf -p no:xdist``.
"""

import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Final

import numpy as np
import pytest
import structlog
import yaml

from hecklr.compose import RunRequest, execute_run, prepare_run
from hecklr_llm import FakeLLM, FakeReply
from hecklr_schemas import Estimate, IntervalMethod, LLMRequest, Trial
from hecklr_sim.testing import decision_reply
from hecklr_stats import quantile_estimates

SC2_LIMIT_S: Final = 600
SCENARIOS: Final = 200
TRIALS: Final = 3
CONCURRENCY: Final = 32
SEED: Final = 20261023
BOOTSTRAP_RESAMPLES: Final = 2000

REPO: Final = Path(__file__).resolve().parents[2]
ACME: Final = REPO / "examples" / "acme-maintenance"
AGENT_START_TIMEOUT_S: Final = 60
_FACT = re.compile(r"^- (\w+): (.+)$", re.MULTILINE)


def models(request: LLMRequest) -> FakeReply:
    """The simulated customer's moves, by how often it spoke so far, and a passing judge."""
    if request.schema_name == "JudgeVerdict":
        return {"verdict": "pass", "rationale": "Scripted verdict.", "evidence": []}
    brief = request.system or ""
    spoken = request.messages[-1].content.count("\nYOU:")
    if spoken == 0:
        return decision_reply(brief.split("## Your goal\n", 1)[1].splitlines()[0])
    if spoken == 1:
        section = brief.split("## Facts you know", 1)[-1].split("\n## ", 1)[0]
        facts = _FACT.findall(section)
        values = " ".join(value for _, value in facts) or "I have no account details."
        return decision_reply(values, facts_revealed=[name for name, _ in facts])
    if spoken < 4:
        return decision_reply("Yes please.")
    return decision_reply("Thanks, bye.", action="end", end_reason="goal_achieved")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@contextmanager
def _rules_agent(log: Path) -> Iterator[str]:
    """The acme agent (rules brain) in its own process; yields its base URL."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    command = [sys.executable, str(ACME / "agent" / "serve.py"), "--brain", "rules"]
    with log.open("wb") as output:
        agent = subprocess.Popen(  # noqa: S603 -- the reference agent, in this interpreter
            [*command, "--port", str(port)], stdout=output, stderr=subprocess.STDOUT
        )
        try:
            _wait_healthy(agent, base_url)
            yield base_url
        finally:
            agent.terminate()
            try:
                agent.wait(timeout=10)
            except subprocess.TimeoutExpired:
                agent.kill()
                agent.wait()


def _wait_healthy(agent: subprocess.Popen[bytes], base_url: str) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + AGENT_START_TIMEOUT_S
    while time.monotonic() < deadline:
        assert agent.poll() is None, f"the agent exited with {agent.returncode}"
        try:
            with opener.open(f"{base_url}/healthz", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    pytest.fail(f"the agent did not answer {base_url}/healthz in time")


def _project(root: Path, agent_url: str) -> Path:
    """The acme world and personas with ``SCENARIOS`` guided scenarios, the acme set repeated."""
    for name in ("worlds", "personas"):
        shutil.copytree(ACME / name, root / name)
    sources = sorted((ACME / "scenarios").glob("**/*.yaml"))
    (root / "scenarios").mkdir()
    for number in range(SCENARIOS):
        scenario = yaml.safe_load(sources[number % len(sources)].read_text("utf-8"))
        scenario["id"] = f"{scenario['id']}-{number // len(sources)}"
        (root / "scenarios" / f"{scenario['id']}.yaml").write_text(yaml.safe_dump(scenario))
    config = yaml.safe_load((ACME / "hecklr.yaml").read_text("utf-8"))
    config["agent"]["adapters"]["chat"]["url"] = f"{agent_url}/chat/stream"
    config["suites"] = {
        "sc2": {"scenarios": "scenarios/*.yaml", "trials": TRIALS, "channels": ["chat"]}
    }
    config["models"] = {"simulator": "fake/customer", "judge": "fake/judge"}
    config["budget"] = {"usd": 0, "concurrency": CONCURRENCY}
    config["seed"] = SEED
    (root / "hecklr.yaml").write_text(yaml.safe_dump(config), "utf-8")
    return root / "hecklr.yaml"


def throughput_estimate(trials: Sequence[Trial], *, seed: int, resamples: int) -> Estimate:
    """Trials finished per second, from the gaps between consecutive finishes.

    The point is trials over the span from the first finish to the last; the interval is a
    bootstrap of the mean gap (``1 / mean gap`` is the rate).
    """
    finishes = np.sort(
        [
            (trial.started_at + timedelta(milliseconds=trial.duration_ms)).timestamp()
            for trial in trials
        ]
    )
    gaps = np.diff(finishes)
    rng = np.random.default_rng(seed)
    means = rng.choice(gaps, size=(resamples, gaps.size)).mean(axis=1)
    low, high = np.quantile(means, [0.025, 0.975])
    return Estimate(
        value=float(1 / gaps.mean()),
        ci_low=float(1 / high),
        ci_high=float(1 / low),
        n=int(gaps.size),
        method=IntervalMethod.BOOTSTRAP,
    )


@pytest.mark.perf
@pytest.mark.e2e
@pytest.mark.fake
@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
@pytest.mark.anyio
async def test_sc2_200_scenarios_x3_trials_under_10_min_at_concurrency_32(tmp_path: Path) -> None:
    logging_config = structlog.get_config()  # the run configures logging for its process
    started = time.monotonic()
    try:
        with _rules_agent(tmp_path / "agent.log") as agent_url:
            config = _project(tmp_path / "project", agent_url)
            prepared = prepare_run(RunRequest(config_path=config, suite="sc2"))
            results = await execute_run(prepared, llm_providers={"fake": FakeLLM(responder=models)})
    finally:
        structlog.configure(**logging_config)
    wall_s = time.monotonic() - started

    trials = results.trials
    assert len({trial.scenario_id for trial in trials}) == SCENARIOS
    assert len(trials) == SCENARIOS * TRIALS
    assert prepared.plan.run.budget.concurrency == CONCURRENCY
    assert {trial.status for trial in trials} == {"ok"}, [t.error for t in trials if t.error]
    assert sum(event.kind == "tool_call" for event in results.events) >= len(trials)
    # "skipped" is a grader that does not apply to the trial; none may have failed to grade.
    assert {grade.status for grade in results.grades} <= {"ok", "skipped"}

    rate = throughput_estimate(trials, seed=SEED, resamples=BOOTSTRAP_RESAMPLES)
    median, p95 = quantile_estimates(
        [trial.duration_ms for trial in trials],
        (0.5, 0.95),
        seed=SEED,
        resamples=BOOTSTRAP_RESAMPLES,
    )
    print(
        f"\nSC2: {len(trials)} trials in {wall_s:.1f} s (limit {SC2_LIMIT_S} s) at concurrency "
        f"{CONCURRENCY}, {sum(t.turns for t in trials) / len(trials):.1f} turns per trial; "
        f"throughput {rate.value:.1f} trials/s "
        f"[{rate.ci_low:.1f}, {rate.ci_high:.1f}] (95% CI); trial duration median "
        f"{median.value:.0f} ms [{median.ci_low:.0f}, {median.ci_high:.0f}], p95 "
        f"{p95.value:.0f} ms [{p95.ci_low:.0f}, {p95.ci_high:.0f}]"
    )
    assert wall_s < SC2_LIMIT_S
