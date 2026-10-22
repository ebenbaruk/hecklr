"""Runs list, run analysis, comparison and trial viewer endpoints, on real stored runs."""

import subprocess
import sys
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest

from hecklr_core import ResultStore
from hecklr_schemas import Estimate

from .conftest import Lab, make_app, store_voice_run

# The lab's runs serve the world sandbox on 127.0.0.1; the API itself is reached over ASGI.
HOLD_DATABASE = """
import sys, time, duckdb
connection = duckdb.connect(sys.argv[1])
print("open", flush=True)
time.sleep(60)
"""

pytestmark = [pytest.mark.anyio, pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]


def _assert_estimate(value: Any) -> Estimate:
    estimate = Estimate.model_validate(value)
    assert estimate.ci_low <= estimate.value <= estimate.ci_high
    return estimate


async def test_runs_list_shows_every_run_newest_first(client: httpx2.AsyncClient, lab: Lab) -> None:
    response = await client.get("/api/runs")

    assert response.status_code == 200
    runs = response.json()
    assert [r["info"]["id"] for r in runs] == [lab.candidate, lab.baseline]
    first = runs[0]
    assert first["info"]["status"] == "completed"
    assert first["info"]["suite"] == "ci"
    assert first["counts"]["ok"] == 10
    assert first["planned"] == 10


async def test_us01_run_summary_reports_pass_rate_and_cost_with_intervals(
    client: httpx2.AsyncClient, lab: Lab
) -> None:
    baseline = (await client.get(f"/api/runs/{lab.baseline}/summary")).json()
    candidate = (await client.get(f"/api/runs/{lab.candidate}/summary")).json()

    assert _assert_estimate(baseline["pass_rate"]).value == 1.0
    worse = _assert_estimate(candidate["pass_rate"])
    assert worse.value == pytest.approx(0.8)
    assert worse.ci_low < worse.value
    _assert_estimate(candidate["cost_per_trial"])
    assert candidate["counts"]["analyzed"] == 10
    assert candidate["problems"] == []


async def test_run_analysis_is_the_reports_analysis(client: httpx2.AsyncClient, lab: Lab) -> None:
    analysis = (await client.get(f"/api/runs/{lab.candidate}")).json()

    assert analysis["candidate"]["id"] == lab.candidate
    assert analysis["baseline"] is None
    saturday = next(s for s in analysis["scenarios"] if s["scenario_id"] == "saturday-visits")
    assert saturday["passes"] == 0
    _assert_estimate(saturday["pass_rate"])
    assert len(analysis["trials"]) == 10


async def test_us05_comparison_has_verdict_deltas_with_ci_and_flips_by_cause(
    client: httpx2.AsyncClient, lab: Lab
) -> None:
    response = await client.get(
        "/api/compare", params={"baseline": lab.baseline, "candidate": lab.candidate}
    )

    assert response.status_code == 200
    analysis = response.json()
    assert analysis["baseline"]["id"] == lab.baseline
    assert analysis["verdict"] in {"regression", "inconclusive"}
    suite = analysis["comparisons"][0]
    assert suite["metric"] == "pass_rate"
    assert suite["slice"] == {}
    delta = _assert_estimate(suite["delta"])
    assert delta.value == pytest.approx(-0.2)
    assert analysis["flips"] == [
        {
            "direction": "pass_to_fail",
            "cause": "judge:tone",
            "scenario_ids": ["saturday-visits"],
            "rationale": "Scripted verdict.",
        }
    ]


async def test_comparing_a_run_with_itself_is_refused(client: httpx2.AsyncClient, lab: Lab) -> None:
    response = await client.get(
        "/api/compare", params={"baseline": lab.baseline, "candidate": lab.baseline}
    )

    assert response.status_code == 422


@pytest.mark.parametrize("run_id", ["01ARZ3NDEKTSV4RRFFQ69G5FAV", "not-a-run", "..%2F..%2Fetc"])
async def test_unknown_or_malformed_runs_are_404(client: httpx2.AsyncClient, run_id: str) -> None:
    response = await client.get(f"/api/runs/{run_id}/summary")

    assert response.status_code == 404
    assert response.json()["detail"].startswith(("no stored run", "no API route"))


async def test_unknown_api_routes_are_json_404(client: httpx2.AsyncClient) -> None:
    response = await client.get("/api/nope")

    assert response.status_code == 404
    assert response.json() == {"detail": "no API route /api/nope"}


async def _trial_of(client: httpx2.AsyncClient, run_id: str, scenario_id: str) -> str:
    analysis = (await client.get(f"/api/runs/{run_id}")).json()
    return str(next(t["trial_id"] for t in analysis["trials"] if t["scenario_id"] == scenario_id))


async def test_us03_trial_viewer_has_transcript_tools_world_diff_and_judge_rationale(
    client: httpx2.AsyncClient, lab: Lab
) -> None:
    trial_id = await _trial_of(client, lab.candidate, "reschedule-visit")

    response = await client.get(f"/api/runs/{lab.candidate}/trials/{trial_id}")

    assert response.status_code == 200
    detail = response.json()
    assert detail["title"] == "Customer moves a plumbing visit to a morning the week after"
    assert detail["row"]["passed"] is True
    kinds = [e["payload"]["kind"] for e in detail["events"]]
    assert "utterance" in kinds
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    times = [(e["t_ms"], e["seq"]) for e in detail["events"]]
    assert times == sorted(times)
    assert [c["collection"] for c in detail["world_changes"]] == ["appointments"]
    assert "slot" in detail["world_changes"][0]["change"]
    judge = next(g for g in detail["grades"] if g["grader"] == "judge:resolved")
    assert judge["rationale"] == "Scripted verdict."
    assert judge["judge_model"] == "fake/judge"
    assert detail["recordings"] == []
    assert detail["labels"] == []


async def test_unknown_trial_is_404(client: httpx2.AsyncClient, lab: Lab) -> None:
    response = await client.get(f"/api/runs/{lab.candidate}/trials/01ARZ3NDEKTSV4RRFFQ69G5FAV")

    assert response.status_code == 404


async def test_us03_voice_trial_has_recordings_with_turn_markers(lab: Lab) -> None:
    run_id, trial_id = await store_voice_run(lab)
    transport = httpx2.ASGITransport(app=make_app(lab))
    async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        detail = (await client.get(f"/api/runs/{run_id}/trials/{trial_id}")).json()

        escaping, mix = detail["recordings"]
        assert escaping["path"] == "../../../secret.wav"
        assert escaping["url"] is None
        assert "outside the run directory" in escaping["note"]
        assert mix["path"] == f"recordings/{trial_id}/mix.wav"
        # start_sample 4000 at 8 kHz = 500 ms into the recording, spoken at trial time 500 ms.
        assert mix["origin_ms"] == 0
        markers = {(m["start_ms"], m["end_ms"], m["actor"], m["kind"]) for m in mix["markers"]}
        assert markers == {
            (500, 1500, "user", "speech"),
            (1900, 2800, "agent", "speech"),
            (2500, 2500, "user", "barge_in"),
        }

        audio = await client.get(mix["url"])
        assert audio.status_code == 200
        assert audio.headers["content-type"] == "audio/wav"
        assert audio.content[:4] == b"RIFF"
        ranged = await client.get(mix["url"], headers={"range": "bytes=0-3"})
        assert ranged.status_code == 206
        assert ranged.content == b"RIFF"


@pytest.mark.parametrize(
    "path",
    ["..%2F..%2F..%2Fsecret.wav", "run.json", "recordings/missing.wav", "%2Fetc%2Fpasswd"],
)
async def test_recordings_outside_the_run_or_not_wav_are_never_served(
    client: httpx2.AsyncClient, lab: Lab, path: str
) -> None:
    (lab.root / "secret.wav").write_bytes(b"RIFF secret")

    response = await client.get(f"/api/runs/{lab.candidate}/recordings/{path}")

    assert response.status_code == 404


@pytest.fixture
def held_database(lab: Lab) -> Iterator[None]:
    """Another process holds the baseline's database for writing, as `hecklr run` does."""
    run_dir = lab.output_dir / "runs" / lab.baseline
    for table in ("trials", "events", "grades"):
        (run_dir / f"{table}.parquet").unlink()
    with subprocess.Popen(  # noqa: S603 - this interpreter and a constant script
        [sys.executable, "-c", HOLD_DATABASE, str(run_dir / "results.duckdb")],
        stdout=subprocess.PIPE,
        text=True,
    ) as writer:
        assert writer.stdout is not None
        assert writer.stdout.readline().strip() == "open"
        yield
        writer.kill()


@pytest.mark.usefixtures("held_database")
async def test_a_run_being_written_is_listed_but_its_results_are_busy(lab: Lab) -> None:
    transport = httpx2.ASGITransport(app=make_app(lab))
    async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        listed = (await client.get("/api/runs")).json()
        summary = await client.get(f"/api/runs/{lab.baseline}/summary")

    assert lab.baseline in [r["info"]["id"] for r in listed]
    assert summary.status_code == 409
    assert "still being written" in summary.json()["detail"]


async def test_a_run_without_exports_is_read_from_its_database_read_only(lab: Lab) -> None:
    run_dir = lab.output_dir / "runs" / lab.baseline
    for table in ("trials", "events", "grades"):
        (run_dir / f"{table}.parquet").unlink()
    before = (run_dir / "results.duckdb").read_bytes()
    transport = httpx2.ASGITransport(app=make_app(lab))
    async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        summary = (await client.get(f"/api/runs/{lab.baseline}/summary")).json()

    assert summary["counts"]["analyzed"] == 10
    assert (run_dir / "results.duckdb").read_bytes() == before
    async with ResultStore(lab.output_dir / "runs") as store:
        assert len((await store.load_run(lab.baseline)).trials) == 10  # still writable after


async def test_a_run_with_no_results_at_all_is_busy(client: httpx2.AsyncClient, lab: Lab) -> None:
    run_dir = lab.output_dir / "runs" / lab.baseline
    for name in ("trials.parquet", "results.duckdb"):
        (run_dir / name).unlink()

    response = await client.get(f"/api/runs/{lab.baseline}/summary")

    assert response.status_code == 409
