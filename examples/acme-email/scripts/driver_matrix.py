"""The email fault matrix over real processes, without ``hecklr run``: a driver-level proof.

For each variant (clean twice for A/A, F11, F12) it starts the email agent as its own process
(``serve.py ... webhook``), runs every scripted scenario as a graded trial through Hecklr's
``webhook`` adapter and ``EmailDriver``, with the agent's tools on a real world ``Sandbox`` over
HTTP, and compares each variant with the first clean run using ``hecklr_stats.compare_runs``::

    uv run python examples/acme-email/scripts/driver_matrix.py [--trials 3]

``email-fault-matrix.sh`` is the same proof through the CLI, once ``hecklr run`` drives email.
"""

import argparse
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import anyio
import httpx2

EXAMPLE: Final = Path(__file__).resolve().parents[1]
for directory in (
    EXAMPLE.parent / "acme-maintenance" / "agent",
    EXAMPLE / "agent",
    EXAMPLE / "tests",
):
    sys.path.insert(0, str(directory))

from acme_agent import PROJECT_ROOT, WORLD_FILE  # noqa: E402 -- importable once the path is set
from email_testing import (  # noqa: E402
    EmailTrial,
    free_port,
    load_scenarios,
    persona_library,
    run_served_email_trial,
)

from hecklr_adapters.email import WebhookEmailTarget  # noqa: E402
from hecklr_channels import EmailDriver  # noqa: E402
from hecklr_channels.email import WEBHOOK_AGENT_ADDR  # noqa: E402
from hecklr_schemas import RunId, StatsConfig, WebhookAdapterConfig, World, new_id  # noqa: E402
from hecklr_stats import TrialOutcome, compare_runs, wilson_estimate  # noqa: E402
from hecklr_world import Sandbox, load_world  # noqa: E402

VARIANTS: Final = ("clean", "clean", "F11", "F12")
REPLY_TIMEOUT_S: Final = 10.0
SEED: Final = 20261022


@contextmanager
def agent_process(variant: str, port: int, world_base_url: str) -> Iterator[str]:
    """The email agent (rules brain) as a separate process, until the block ends."""
    fault = [] if variant == "clean" else ["--fault", variant]
    command = [
        sys.executable, str(EXAMPLE / "agent" / "serve.py"), "--brain", "rules", *fault,
        "--world-base-url", world_base_url, "webhook", "--port", str(port),
    ]  # fmt: skip
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603 -- fixed arguments
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while not _healthy(url):
            if time.monotonic() > deadline or process.poll() is not None:
                raise RuntimeError(f"the {variant} agent did not start")
            time.sleep(0.2)
        yield url
    finally:
        process.terminate()
        process.wait(timeout=10)


def _healthy(url: str) -> bool:
    try:
        return httpx2.get(f"{url}/healthz", timeout=1).status_code == 200
    except httpx2.HTTPError:
        return False


async def run_variant(variant: str, world: World, trials: int) -> list[EmailTrial]:
    """Every scripted scenario ``trials`` times against one agent process."""
    scenarios = load_scenarios("scripted/*.yaml")
    personas = persona_library()
    results = []
    async with Sandbox() as sandbox:
        with agent_process(variant, free_port(), sandbox.base_url) as url:
            config = WebhookAdapterConfig.model_validate(
                {"type": "webhook", "url": f"{url}/email", "listen_port": free_port()}
            )
            target = WebhookEmailTarget(config)
            driver = EmailDriver(
                target, agent_addr=WEBHOOK_AGENT_ADDR, reply_timeout_s=REPLY_TIMEOUT_S
            )
            try:
                for scenario in scenarios:
                    persona = personas[scenario.persona_ids[0]]
                    for _ in range(trials):
                        results.append(
                            await run_served_email_trial(scenario, persona, world, sandbox, driver)
                        )
            finally:
                target.close()
            health = httpx2.get(f"{url}/healthz").json()
    _say(f"  agent /healthz: {health}")
    return results


def _say(line: str) -> None:
    sys.stdout.write(f"{line}\n")


def outcomes(trials: Sequence[EmailTrial]) -> list[TrialOutcome]:
    """The trials as the statistics read them (a trial that timed out failed)."""
    return [
        TrialOutcome(
            scenario_id=trial.trial.scenario_id,
            passed=trial.passed,
            labels={"channel": "email"},
        )
        for trial in trials
    ]


async def main(argv: Sequence[str] | None = None) -> None:
    """Run every variant, then compare each with the first clean run."""
    args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args.add_argument("--trials", type=int, default=1)
    trials = args.parse_args(argv).trials
    world = load_world(PROJECT_ROOT / WORLD_FILE)
    runs: list[tuple[str, RunId, list[EmailTrial]]] = []
    for variant in VARIANTS:
        _say(f"== {variant}")
        results = await run_variant(variant, world, trials)
        passed = sum(trial.passed for trial in results)
        rate = wilson_estimate(passed, len(results))
        _say(
            f"  pass rate {passed}/{len(results)} = {rate.value:.2f} "
            f"[{rate.ci_low:.2f}, {rate.ci_high:.2f}]"
        )
        causes = Counter(cause for trial in results for cause in trial.failed())
        _say(f"  failure causes: {dict(causes) or 'none'}")
        runs.append((variant, RunId(new_id()), results))

    _, baseline_id, baseline = runs[0]
    for variant, run_id, results in runs[1:]:
        [suite, *_] = compare_runs(
            outcomes(baseline),
            outcomes(results),
            baseline_run=baseline_id,
            candidate_run=run_id,
            config=StatsConfig(),
            seed=SEED,
        )
        delta = suite.delta
        _say(
            f"== clean vs {variant}: verdict {suite.verdict.value}, delta {delta.value:+.2f} "
            f"[{delta.ci_low:+.2f}, {delta.ci_high:+.2f}], p = {suite.p_value:.4f}"
        )


if __name__ == "__main__":
    anyio.run(main)
