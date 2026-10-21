"""Programmatic API: run a suite from Python, as ``hecklr run`` does from the shell."""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from functools import partial
from pathlib import Path

import anyio

from hecklr.compose import RunRequest, execute_run, prepare_run
from hecklr_core import RunResults
from hecklr_schemas import Channel, LLMProvider


async def run_async(
    config_path: str | Path = "hecklr.yaml",
    *,
    suite: str,
    channels: Sequence[Channel | str] | None = None,
    trials: int | None = None,
    budget_usd: Decimal | float | None = None,
    seed: int | None = None,
    adaptive: bool | None = None,
    baseline: str | None = None,
    output_dir: str | Path | None = None,
    llm_providers: Mapping[str, LLMProvider] | None = None,
) -> RunResults:
    """Run ``suite`` of the project at ``config_path`` and return its stored results.

    Arguments mirror the ``hecklr run`` flags. ``llm_providers`` pins a provider per model prefix,
    e.g. ``{"fake": FakeLLM(...)}`` in tests.

    Raises:
        The configuration and budget errors listed on ``hecklr.compose.prepare_run``.
    """
    request = RunRequest(
        config_path=Path(config_path),
        suite=suite,
        channels=[Channel(channel) for channel in channels] if channels is not None else None,
        trials=trials,
        budget_usd=Decimal(str(budget_usd)) if budget_usd is not None else None,
        seed=seed,
        adaptive=adaptive,
        baseline_id=baseline,
        output_dir=Path(output_dir) if output_dir is not None else None,
    )
    return await execute_run(prepare_run(request), llm_providers=llm_providers)


def run(
    config_path: str | Path = "hecklr.yaml",
    *,
    suite: str,
    channels: Sequence[Channel | str] | None = None,
    trials: int | None = None,
    budget_usd: Decimal | float | None = None,
    seed: int | None = None,
    adaptive: bool | None = None,
    baseline: str | None = None,
    output_dir: str | Path | None = None,
    llm_providers: Mapping[str, LLMProvider] | None = None,
) -> RunResults:
    """Blocking ``run_async``, for scripts and notebooks without an event loop running."""
    return anyio.run(
        partial(
            run_async,
            config_path,
            suite=suite,
            channels=channels,
            trials=trials,
            budget_usd=budget_usd,
            seed=seed,
            adaptive=adaptive,
            baseline=baseline,
            output_dir=output_dir,
            llm_providers=llm_providers,
        )
    )
