"""The full SC7 pipeline on the calibration set, offline: cases -> judge prompts (FakeLLM) ->
grades -> Cohen's kappa with a bootstrap interval, per rubric, and ``hecklr calibrate --judges``.

The scripted judges have a known relation to the labels, so the kappa they must get is known:
a judge agreeing with every label gets 1, a coin flip gets about 0, a judge that flips exactly the
borderline cases gets something in between.
"""

import random
from collections.abc import Callable
from pathlib import Path

import anyio
import click
import pytest
from typer.testing import CliRunner

import hecklr.commands._common
from hecklr import exit_codes
from hecklr.cli import app
from hecklr.commands._common import CliOverrides
from hecklr.judge_calibration import CALIBRATED_RUBRICS, CalibrationSet, grader_name, judge_set
from hecklr.scaffold import AgentChoice, ModelChoice, write_project
from hecklr_llm import FakeLLM
from hecklr_schemas import Grade, HumanLabel
from hecklr_stats import JudgeCalibration, calibrate_judge

SET_DIR = Path(__file__).resolve().parents[2] / "calibration" / "judges"
Verdict = Callable[[str, str, bool], bool]
RESAMPLES = 2_000
SEED = 7


def perfect(case_id: str, rubric: str, label: bool) -> bool:
    return label


def coin_flip(case_id: str, rubric: str, label: bool) -> bool:
    return random.Random(f"{SEED}:{case_id}:{rubric}").random() < 0.5


def _borderline(calibration_set: CalibrationSet) -> set[tuple[str, str]]:
    return {
        (case.id, name)
        for case in calibration_set.cases
        for name, use in case.rubrics.items()
        if use.borderline
    }


def _calibrate(
    calibration_set: CalibrationSet, judge: FakeLLM, labels: list[HumanLabel]
) -> dict[str, JudgeCalibration]:
    grades: list[Grade] = anyio.run(judge_set, calibration_set, judge, "fake/judge")
    assert all(grade.status == "ok" for grade in grades)
    return {
        name: calibrate_judge(
            [grade for grade in grades if grade.grader == grader_name(name)],
            labels,
            seed=SEED,
            resamples=RESAMPLES,
        )
        for name in CALIBRATED_RUBRICS
    }


def test_sc7_a_judge_agreeing_with_every_label_has_kappa_1_on_every_rubric(
    calibration_set: CalibrationSet,
    draft_labels: list[HumanLabel],
    scripted_judge: Callable[[Verdict], FakeLLM],
) -> None:
    judge = scripted_judge(perfect)

    calibrations = _calibrate(calibration_set, judge, draft_labels)

    assert len(judge.calls) == sum(1 for _ in calibration_set.items())
    for name, calibration in calibrations.items():
        assert calibration.kappa.value == pytest.approx(1.0), name
        assert calibration.kappa.ci_low == pytest.approx(1.0), name
        assert calibration.trusted
        assert calibration.n >= 40


def test_sc7_a_coin_flip_judge_has_kappa_near_0_and_is_untrusted(
    calibration_set: CalibrationSet,
    draft_labels: list[HumanLabel],
    scripted_judge: Callable[[Verdict], FakeLLM],
) -> None:
    calibrations = _calibrate(calibration_set, scripted_judge(coin_flip), draft_labels)

    kappas = {name: calibration.kappa.value for name, calibration in calibrations.items()}
    assert all(abs(kappa) < 0.35 for kappa in kappas.values()), kappas
    assert abs(sum(kappas.values()) / len(kappas)) < 0.15, kappas
    assert not any(calibration.trusted for calibration in calibrations.values())


def test_sc7_a_judge_wrong_on_the_borderline_cases_gets_an_intermediate_kappa(
    calibration_set: CalibrationSet,
    draft_labels: list[HumanLabel],
    scripted_judge: Callable[[Verdict], FakeLLM],
) -> None:
    borderline = _borderline(calibration_set)

    def flipped(case_id: str, rubric: str, label: bool) -> bool:
        return label != ((case_id, rubric) in borderline)

    calibrations = _calibrate(calibration_set, scripted_judge(flipped), draft_labels)

    for name, calibration in calibrations.items():
        kappa = calibration.kappa
        assert 0.2 < kappa.value < 0.95, (name, kappa)
        assert kappa.ci_low < kappa.value < kappa.ci_high, (name, kappa)


# --- the CLI ------------------------------------------------------------------------------------


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project whose judge is ``fake/judge``, with fast, seeded statistics."""
    root = tmp_path / "project"
    write_project(
        root, "acme", AgentChoice("python"), ModelChoice("fake/customer", "fake/judge", "tests")
    )
    config = root / "hecklr.yaml"
    text = config.read_text(encoding="utf-8")
    old = "stats: { alpha: 0.05, min_effect: 0.03 }"
    assert old in text
    config.write_text(
        text.replace(
            old,
            "stats: { alpha: 0.05, min_effect: 0.03, "
            f"bootstrap_resamples: {RESAMPLES}, seed: {SEED} }}",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(hecklr.commands._common.stdout, "width", 200)
    return config


def _calibrate_cli(config: Path, judge: FakeLLM, *args: str) -> tuple[int, str]:
    result = CliRunner().invoke(
        app,
        ["calibrate", "--judges", "--set", str(SET_DIR), "--config", str(config), *args],
        obj=CliOverrides({"fake": judge}),
    )
    return result.exit_code, " ".join(click.unstyle(result.output).replace("│", " ").split())


def test_sc7_calibrate_judges_set_trusts_a_judge_that_agrees_with_the_labels(
    config_path: Path, scripted_judge: Callable[[Verdict], FakeLLM]
) -> None:
    code, output = _calibrate_cli(config_path, scripted_judge(perfect))

    assert code == exit_codes.OK, output
    for name in CALIBRATED_RUBRICS:
        assert f"{grader_name(name)} " in output
    assert output.count("1.00 [1.00, 1.00]") == len(CALIBRATED_RUBRICS)
    assert "NO (kappa < 0.70)" not in output
    # The shipped labels are AI drafts: the command says they are not SC7 evidence.
    assert "are not from 'human:' labelers (claude-draft)" in output


def test_sc7_calibrate_judges_set_fails_a_coin_flip_judge(
    config_path: Path, scripted_judge: Callable[[Verdict], FakeLLM]
) -> None:
    code, output = _calibrate_cli(config_path, scripted_judge(coin_flip))

    assert code == exit_codes.CHECK_FAILED, output
    assert output.count("NO (kappa < 0.70)") == len(CALIBRATED_RUBRICS)


def test_sc7_calibrate_judges_set_with_only_human_labels_reports_every_rubric_uncalibrated(
    config_path: Path, scripted_judge: Callable[[Verdict], FakeLLM]
) -> None:
    # No person has reviewed the shipped drafts yet: with --labeler human: nothing is calibrated.
    code, output = _calibrate_cli(config_path, scripted_judge(perfect), "--labeler", "human:")

    assert code == exit_codes.CHECK_FAILED, output
    assert output.count("not calibrated, no label of its current rubric version") == len(
        CALIBRATED_RUBRICS
    )
