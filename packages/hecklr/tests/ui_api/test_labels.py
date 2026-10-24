"""Judge labeling queue, human labels and judge-human agreement (US-11, SC7)."""

from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import anyio.to_thread
import click
import httpx2
import pytest
from typer.testing import CliRunner

from hecklr import exit_codes
from hecklr.cli import app as cli_app
from hecklr.ui_api.labels import (
    LabelBook,
    LabelFileError,
    UnknownGradeError,
    agreement,
    find_judge_grade,
    label_queue,
)
from hecklr_core import RunResults
from hecklr_schemas import (
    AgentRef,
    Budget,
    Channel,
    Estimate,
    Grade,
    GradeId,
    HumanLabel,
    Run,
    RunId,
    Trial,
    TrialId,
    WorldSnapshot,
    new_id,
)

from .conftest import NOW, USER, Lab

pytestmark = [pytest.mark.anyio, pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]


async def _queue(client: httpx2.AsyncClient, **params: Any) -> Any:
    response = await client.get("/api/labels/queue", params={"labeler": USER, **params})
    assert response.status_code == 200, response.text
    return response.json()


async def test_us11_queue_offers_judge_grades_of_judged_runs_stratified_by_rubric(
    client: httpx2.AsyncClient, lab: Lab
) -> None:
    queue = await _queue(client, limit=6)

    assert set(queue["runs"]) == {lab.baseline, lab.candidate}
    assert queue["graders"] == ["judge:no_hallucination", "judge:resolved", "judge:tone"]
    items = queue["items"]
    assert isinstance(items, list)
    assert len(items) == 6
    assert Counter(i["grade"]["grader"] for i in items) == {
        "judge:no_hallucination": 2,
        "judge:resolved": 2,
        "judge:tone": 2,
    }
    first = items[0]
    assert first["label"] is None
    assert first["title"]
    assert {e["payload"]["kind"] for e in first["transcript"]} == {"utterance"}
    assert queue["labeled"] == 0


async def test_queue_is_reproducible_and_filters_by_rubric_and_run(
    client: httpx2.AsyncClient, lab: Lab
) -> None:
    once = await _queue(client, run=lab.candidate, grader="judge:tone")
    again = await _queue(client, run=lab.candidate, grader="judge:tone")

    assert once == again
    assert once["runs"] == [lab.candidate]
    assert {i["grade"]["grader"] for i in once["items"]} == {"judge:tone"}


async def test_us11_label_is_appended_as_a_human_label_and_shown_in_the_queue(
    client: httpx2.AsyncClient, lab: Lab
) -> None:
    item = (await _queue(client, limit=1))["items"][0]

    response = await client.post(
        "/api/labels",
        json={
            "run_id": item["run_id"],
            "grade_id": item["grade"]["id"],
            "passed": False,
            "labeler": USER,
            "note": "the agent never confirmed the slot",
        },
    )

    assert response.status_code == 201
    label = HumanLabel.model_validate(response.json())
    assert label.grade_id == item["grade"]["id"]
    assert label.grader == item["grade"]["grader"]
    assert label.grader_version == item["grade"]["grader_version"]
    assert label.passed is False
    lines = (lab.output_dir / "labels.jsonl").read_text(encoding="utf-8").splitlines()
    assert [HumanLabel.model_validate_json(line) for line in lines] == [label]
    queue = await _queue(client, limit=50)
    labeled = [i for i in queue["items"] if i["label"] is not None]
    assert [i["grade"]["id"] for i in labeled] == [item["grade"]["id"]]
    assert queue["items"][-1]["label"]["note"] == "the agent never confirmed the slot"
    assert queue["labeled"] == 1


async def test_labels_of_unknown_grades_are_refused(client: httpx2.AsyncClient, lab: Lab) -> None:
    response = await client.post(
        "/api/labels",
        json={"run_id": lab.baseline, "grade_id": new_id(), "passed": True, "labeler": USER},
    )

    assert response.status_code == 404
    assert not (lab.output_dir / "labels.jsonl").exists()


async def test_labels_need_a_labeler(client: httpx2.AsyncClient, lab: Lab) -> None:
    item = (await _queue(client, limit=1))["items"][0]

    response = await client.post(
        "/api/labels",
        json={
            "run_id": lab.baseline,
            "grade_id": item["grade"]["id"],
            "passed": True,
            "labeler": "",
        },
    )

    assert response.status_code == 422


async def test_sc7_agreement_reports_kappa_precision_recall_with_intervals(
    client: httpx2.AsyncClient, lab: Lab
) -> None:
    queue = await _queue(client, run=lab.candidate, grader="judge:tone", limit=10)
    for index, item in enumerate(queue["items"]):
        # The human disagrees with the judge on the first trial only.
        human = item["grade"]["passed"] != (index == 0)
        posted = await client.post(
            "/api/labels",
            json={
                "run_id": item["run_id"],
                "grade_id": item["grade"]["id"],
                "passed": human,
                "labeler": USER,
            },
        )
        assert posted.status_code == 201

    report = (await client.get("/api/labels/agreement")).json()

    assert report["labels"] == 2
    assert report["unmatched"] == 0
    (judge,) = report["judges"]
    assert judge["grader"] == "judge:tone"
    assert judge["judge_model"] == "fake/judge"
    assert judge["n"] == 2
    kappa = Estimate.model_validate(judge["kappa"])
    assert kappa.ci_low <= kappa.value <= kappa.ci_high
    assert judge["trusted"] is (kappa.value >= 0.7)


async def test_the_labels_file_is_what_hecklr_calibrate_judges_reads(
    client: httpx2.AsyncClient, lab: Lab
) -> None:
    queue = await _queue(client, run=lab.candidate, limit=30)
    for item in queue["items"]:
        await client.post(
            "/api/labels",
            json={
                "run_id": item["run_id"],
                "grade_id": item["grade"]["id"],
                "passed": item["grade"]["passed"],
                "labeler": USER,
            },
        )
    ui_report = (await client.get("/api/labels/agreement")).json()

    args = [
        "calibrate", "--judges", "--config", str(lab.config),
        "--labels", str(lab.output_dir / "labels.jsonl"), "--run", lab.candidate,
    ]  # fmt: skip
    # The CLI runs its own event loop, so it cannot run on this test's.
    result = await anyio.to_thread.run_sync(lambda: CliRunner().invoke(cli_app, args))

    # The CLI matched these labels to the run's judge grades (it refuses labels that match none)
    # and, as the UI did, found perfect agreement on too few labels of each class to trust
    # (issue #100).
    output = click.unstyle(result.output)
    assert result.exit_code == exit_codes.CHECK_FAILED, output
    assert "Judge agreement with human labels" in output
    assert {j["grader"]: j["n"] for j in ui_report["judges"]} == {
        "judge:no_hallucination": 4,
        "judge:resolved": 4,
        "judge:tone": 2,
    }
    judges = ui_report["judges"]
    assert all(judge["kappa"]["value"] == 1.0 and not judge["trusted"] for judge in judges)


# --- unit level ----------------------------------------------------------------------------

_WORLD = WorldSnapshot(world_id="w", taken_at_ms=0, collections={})


def _results(graders: dict[str, int], *, unjudged: frozenset[str] = frozenset()) -> RunResults:
    """``graders[name]`` ``ok`` grades each; the ``unjudged`` ones decided without a judge."""
    run = Run(
        id=RunId(new_id()),
        created_at=NOW,
        agent=AgentRef(name="a"),
        config_hash="0",
        seed=1,
        matrix=[],
        budget=Budget(),
    )
    trials: list[Trial] = []
    grades: list[Grade] = []
    for grader, count in graders.items():
        for index in range(count):
            trial = Trial(
                id=TrialId(new_id()),
                run_id=run.id,
                scenario_id=f"s{index}",
                persona_id="p",
                channel=Channel.CHAT,
                index=0,
                seed=index,
                status="ok",
                started_at=NOW,
                duration_ms=1,
                cost_usd=Decimal(0),
                world_before=_WORLD,
                world_after=_WORLD,
            )
            trials.append(trial)
            grades.append(
                Grade(
                    id=GradeId(new_id()),
                    trial_id=trial.id,
                    grader=grader,
                    grader_version="1",
                    value=True,
                    passed=True,
                    judge_model=None if grader in unjudged else "fake/judge",
                )
            )
    return RunResults(run=run, trials=trials, events=[], grades=grades)


def test_queue_interleaves_rubrics_so_a_prefix_covers_each_equally() -> None:
    results = _results(
        {"judge:a": 10, "judge:b": 2, "judge:c": 10, "tool_called": 5},
        unjudged=frozenset({"tool_called"}),
    )

    items, labeled = label_queue([results], {}, [], labeler=USER, grader=None, limit=7, seed=3)

    assert [i.grade.grader for i in items[:6]].count("judge:b") == 2
    assert Counter(i.grade.grader for i in items) == {"judge:a": 3, "judge:b": 2, "judge:c": 2}
    assert labeled == 0
    shuffled, _ = label_queue([results], {}, [], labeler=USER, grader=None, limit=7, seed=4)
    assert [i.grade.id for i in shuffled] != [i.grade.id for i in items]


_SAFETY_GRADERS_ASKING_A_JUDGE = ("injection_resisted", "no_policy_override", "no_pii_leak")


def test_issue87_safety_graders_that_ask_a_judge_are_in_the_queue() -> None:
    judged = _results(dict.fromkeys(_SAFETY_GRADERS_ASKING_A_JUDGE, 2))
    # Their deterministic pre-checks (a forbidden string, a leaked phone number) ask no judge.
    prechecked = _results(
        dict.fromkeys(_SAFETY_GRADERS_ASKING_A_JUDGE, 3),
        unjudged=frozenset(_SAFETY_GRADERS_ASKING_A_JUDGE),
    )

    items, _ = label_queue(
        [judged, prechecked], {}, [], labeler=USER, grader=None, limit=50, seed=0
    )

    assert sorted(i.grade.id for i in items) == sorted(g.id for g in judged.grades)
    assert Counter(i.grade.grader for i in items) == dict.fromkeys(
        _SAFETY_GRADERS_ASKING_A_JUDGE, 2
    )


def test_issue87_a_judged_safety_grade_can_be_labeled_but_not_its_pre_check() -> None:
    judged = _results({"injection_resisted": 1}).grades[0]
    prechecked = _results(
        {"injection_resisted": 1}, unjudged=frozenset({"injection_resisted"})
    ).grades[0]
    results = _results({})
    both = RunResults(run=results.run, trials=[], events=[], grades=[judged, prechecked])

    assert find_judge_grade(both, judged.id) == judged
    with pytest.raises(UnknownGradeError):
        find_judge_grade(both, prechecked.id)


def test_relabeling_keeps_the_newest_label_and_other_labelers_do_not_count(tmp_path: Path) -> None:
    results = _results({"judge:a": 1})
    grade = results.grades[0]
    book = LabelBook(tmp_path / "labels.jsonl")
    book.add(grade, passed=True, labeler=USER, note=None, now=NOW)
    newest = book.add(
        grade, passed=False, labeler=USER, note="on reflection", now=NOW.replace(hour=13)
    )
    book.add(grade, passed=True, labeler="bob", note=None, now=NOW.replace(hour=14))

    items, labeled = label_queue(
        [results], {}, book.all(), labeler=USER, grader=None, limit=5, seed=0
    )

    assert items[0].label == newest
    assert labeled == 1


def test_agreement_ignores_labels_without_a_stored_grade(tmp_path: Path) -> None:
    results = _results({"judge:a": 3})
    book = LabelBook(tmp_path / "labels.jsonl")
    for grade in results.grades:
        book.add(grade, passed=True, labeler=USER, note=None, now=NOW)
    stray = _results({"judge:a": 1}).grades[0]
    book.add(stray, passed=False, labeler=USER, note=None, now=NOW)

    report = agreement(results.grades, book.all(), seed=0, resamples=200)

    assert report.labels == 4
    assert report.unmatched == 1
    assert report.judges[0].n == 3
    assert report.judges[0].kappa.value == 1.0


def test_a_hand_broken_labels_file_is_reported_with_its_line(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    path.write_text("\n{not json}\n", encoding="utf-8")

    with pytest.raises(LabelFileError, match=r"labels.jsonl:2"):
        LabelBook(path).all()


async def test_a_run_still_being_written_is_left_out_of_the_queue_and_agreement(
    client: httpx2.AsyncClient, lab: Lab
) -> None:
    run_dir = lab.output_dir / "runs" / lab.baseline
    for name in ("trials.parquet", "results.duckdb"):
        (run_dir / name).unlink()

    queue = await _queue(client)
    report = await client.get("/api/labels/agreement")

    assert queue["items"]
    assert {item["run_id"] for item in queue["items"]} == {lab.candidate}
    assert report.status_code == 200
