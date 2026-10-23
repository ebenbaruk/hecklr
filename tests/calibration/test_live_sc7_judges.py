"""SC7 acceptance: the real judge model agrees with human labels, kappa >= 0.70 on every rubric.

Paid: ``pytest -m live tests/calibration`` runs it only when ``OPENAI_API_KEY`` is set and
``HECKLR_TEST_BUDGET_USD`` covers the estimated cost (``test_sc7_live_run_cost_estimate``: 434
calls, about 2.7M input tokens, about $0.33 on ``openai/gpt-6-luna``; calibration/judges/GUIDE.md).
It counts
labels of ``HECKLR_SC7_LABELERS`` (comma-separated prefixes, default ``human:``) and skips while no
person has labeled the set: agreement with AI drafts is not SC7 evidence.
"""

import os
from collections.abc import Callable
from decimal import Decimal

import anyio
import pytest

from hecklr.judge_calibration import (
    CALIBRATED_RUBRICS,
    CalibrationSet,
    by_labeler,
    grader_name,
    judge_set,
    read_labels,
)
from hecklr_llm import FakeLLM, ProviderRouter, estimate_input_tokens, estimate_request_cost
from hecklr_schemas import Grade, LLMRequest
from hecklr_stats import KAPPA_TRUST_THRESHOLD, calibrate_judge

MODEL = os.environ.get("HECKLR_LIVE_OPENAI_MODEL", "openai/gpt-6-luna")
LABELERS = os.environ.get("HECKLR_SC7_LABELERS", "human:").split(",")
OUTPUT_TOKENS = 250
"""A judge verdict: a JSON object with a short rationale and a few evidence ids."""
COST_CAP = Decimal("1.00")
"""The documented ceiling for one full live run (GUIDE.md); the estimate must stay below it."""


def _requests(calibration_set: CalibrationSet) -> list[LLMRequest]:
    """The exact judge requests a live run sends, captured from a FakeLLM run of the set."""
    recorder = FakeLLM(
        responder=lambda request: {"verdict": "pass", "rationale": "x", "evidence": []}
    )
    anyio.run(judge_set, calibration_set, recorder, MODEL)
    return recorder.calls


def _estimated_cost(requests: list[LLMRequest]) -> Decimal:
    return sum(
        (estimate_request_cost(r, expected_output_tokens=OUTPUT_TOKENS) for r in requests),
        Decimal(0),
    )


def test_sc7_live_run_cost_estimate(calibration_set: CalibrationSet) -> None:
    requests = _requests(calibration_set)
    input_tokens = sum(estimate_input_tokens(request) for request in requests)
    cost = _estimated_cost(requests)

    print(f"{len(requests)} judge calls, ~{input_tokens:,} input tokens, ~${cost:.3f} on {MODEL}")
    assert len(requests) == sum(1 for _ in calibration_set.items())
    assert cost < COST_CAP


@pytest.mark.live
def test_sc7_live_judges_agree_with_human_labels_on_every_rubric(
    calibration_set: CalibrationSet, record_property: Callable[[str, object], None]
) -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY is not set")
    cost = _estimated_cost(_requests(calibration_set))
    if Decimal(os.environ.get("HECKLR_TEST_BUDGET_USD", "0")) < cost:
        pytest.skip(f"HECKLR_TEST_BUDGET_USD does not cover the estimated ${cost:.2f}")
    labels = by_labeler(read_labels(calibration_set.labels_path), LABELERS)
    unlabeled = set(map(grader_name, CALIBRATED_RUBRICS)) - {label.grader for label in labels}
    if unlabeled:
        pytest.skip(
            f"no label by {LABELERS} yet for {sorted(unlabeled)}: review the set "
            "(calibration/judges/GUIDE.md)"
        )

    async def judge() -> list[Grade]:
        async with ProviderRouter() as router:
            return await judge_set(calibration_set, router, MODEL)

    grades = anyio.run(judge)
    spent = sum((grade.cost_usd for grade in grades), Decimal(0))
    record_property("judge_spend_usd", str(spent))
    kappas = {}
    for name in CALIBRATED_RUBRICS:
        ok = [g for g in grades if g.grader == grader_name(name) and g.status == "ok"]
        calibration = calibrate_judge(ok, labels, seed=0, resamples=10_000)
        kappas[name] = calibration.kappa
        record_property(f"kappa_{name}", calibration.kappa.model_dump_json())
    print({name: f"{k.value:.2f} [{k.ci_low:.2f}, {k.ci_high:.2f}]" for name, k in kappas.items()})
    assert spent <= cost * 2
    assert all(k.value >= KAPPA_TRUST_THRESHOLD for k in kappas.values()), kappas
