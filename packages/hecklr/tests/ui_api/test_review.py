"""Failure clusters and the scenario approval queue over the miner's files (US-08, ADR 0016a)."""

import httpx2
import pytest

from hecklr.compose import load_run_project
from hecklr_core import resolve_suite
from hecklr_schemas import ProposalStatus, ScenarioProposal

from .conftest import USER, Lab, Mined, make_app, write_mine

pytestmark = [pytest.mark.anyio, pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]


async def _mined(client: httpx2.AsyncClient, lab: Lab) -> Mined:
    analysis = (await client.get(f"/api/runs/{lab.candidate}")).json()
    failed = [t["trial_id"] for t in analysis["trials"] if t["passed"] is False]
    return write_mine(lab, failed)


CI_SUITE = (
    'ci: { scenarios: "scenarios/**/*.yaml", tags: [core, mined], trials: 3, channels: [chat] }'
)


def _set_suites(lab: Lab, *suites: str) -> None:
    """Replace the starter's ``ci`` suite with ``suites`` (one-line YAML flow mappings)."""
    config = lab.config.read_text(encoding="utf-8")
    assert CI_SUITE in config
    lab.config.write_text(config.replace(CI_SUITE, "\n  ".join(suites)), encoding="utf-8")


def _client(lab: Lab) -> httpx2.AsyncClient:
    """A client for ``lab`` as configured now (the ``client`` fixture loads it before the test)."""
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=make_app(lab)), base_url="http://127.0.0.1"
    )


def _records(mined: Mined) -> list[ScenarioProposal]:
    lines = mined.proposals_file.read_text(encoding="utf-8").splitlines()
    return [ScenarioProposal.model_validate_json(line) for line in lines]


async def test_no_miner_output_means_empty_lists(client: httpx2.AsyncClient) -> None:
    assert (await client.get("/api/clusters")).json() == []
    assert (await client.get("/api/proposals")).json() == []


async def test_us08_clusters_list_newest_first_with_their_proposals(
    client: httpx2.AsyncClient, lab: Lab
) -> None:
    mined = await _mined(client, lab)

    clusters = (await client.get("/api/clusters")).json()

    assert [c["cluster"]["id"] for c in clusters] == [
        mined.production_cluster.id,
        mined.run_cluster.id,
    ]
    assert [p["id"] for p in clusters[0]["proposals"]] == [p.id for p in mined.proposals]
    assert clusters[1]["proposals"] == []


async def test_cluster_detail_links_trials_to_their_runs(
    client: httpx2.AsyncClient, lab: Lab
) -> None:
    mined = await _mined(client, lab)

    detail = (await client.get(f"/api/clusters/{mined.run_cluster.id}")).json()

    members = detail["members"]
    assert len(members) == 2
    assert members[0]["exemplar"] is True
    assert {m["run_id"] for m in members} == {lab.candidate}


async def test_production_cluster_shows_saved_conversations(
    client: httpx2.AsyncClient, lab: Lab
) -> None:
    mined = await _mined(client, lab)

    detail = (await client.get(f"/api/clusters/{mined.production_cluster.id}")).json()

    exemplar, other = detail["members"]
    assert exemplar["id"] == "prod-1"
    assert exemplar["conversation"]["turns"][1]["text"] == "Booked at 3 rue de la Paix."
    assert other == {"id": "prod-2", "exemplar": False, "run_id": None, "conversation": None}
    assert len(detail["proposals"]) == 2


async def test_unknown_cluster_is_404(client: httpx2.AsyncClient) -> None:
    assert (await client.get("/api/clusters/01ARZ3NDEKTSV4RRFFQ69G5FAV")).status_code == 404


async def test_proposals_queue_shows_yaml_cluster_label_and_source(
    client: httpx2.AsyncClient, lab: Lab
) -> None:
    mined = await _mined(client, lab)

    proposals = (await client.get("/api/proposals", params={"status": "proposed"})).json()

    assert [p["proposal"]["id"] for p in proposals] == [p.id for p in mined.proposals]
    first = proposals[0]
    assert first["cluster_label"] == "fails when the customer gives two addresses"
    assert first["source"] == "2026-10-22/proposals.jsonl"
    assert first["yaml"].startswith("id: mined-double-address\n")


async def test_us08_approving_writes_a_scenario_the_suite_loader_accepts(
    client: httpx2.AsyncClient, lab: Lab
) -> None:
    mined = await _mined(client, lab)
    proposal = mined.proposals[0]

    response = await client.post(
        f"/api/proposals/{proposal.id}/decision",
        json={"decision": "approve", "reviewer": USER, "note": "real bug"},
    )

    assert response.status_code == 200, response.text
    result = response.json()
    written = lab.root / "scenarios" / "mined" / "mined-double-address.yaml"
    assert result["written"] == str(written)
    suite = resolve_suite(load_run_project(lab.config), "ci")
    assert "mined-double-address" in suite.scenarios
    decided = _records(mined)[-1]
    assert decided.id == proposal.id
    assert decided.status is ProposalStatus.APPROVED
    assert decided.reviewer == USER
    assert decided.note == "real bug"
    assert decided.decided_at is not None
    assert len(_records(mined)) == 3  # appended, never rewritten
    approved = (await client.get("/api/proposals", params={"status": "approved"})).json()
    assert [p["proposal"]["id"] for p in approved] == [proposal.id]


async def test_approving_an_edited_scenario_writes_the_edit(
    client: httpx2.AsyncClient, lab: Lab
) -> None:
    mined = await _mined(client, lab)
    proposal = (await client.get("/api/proposals")).json()[0]
    edited = proposal["yaml"].replace(
        "title: Customer gives two addresses", "title: Two addresses, reviewed"
    )

    response = await client.post(
        f"/api/proposals/{mined.proposals[0].id}/decision",
        json={"decision": "approve", "reviewer": USER, "yaml": edited},
    )

    assert response.status_code == 200, response.text
    written = lab.root / "scenarios" / "mined" / "mined-double-address.yaml"
    assert written.read_text(encoding="utf-8") == edited
    assert _records(mined)[-1].scenario.title == "Two addresses, reviewed"


@pytest.mark.parametrize(
    ("yaml", "problem"),
    [
        ("id: x\ntitle: [unclosed\n", "invalid YAML"),
        ("id: x\ntitle: t\n", "channels"),
        ("{}", "id"),
    ],
)
async def test_invalid_yaml_is_refused_with_its_problems(
    client: httpx2.AsyncClient, lab: Lab, yaml: str, problem: str
) -> None:
    mined = await _mined(client, lab)

    response = await client.post(
        f"/api/proposals/{mined.proposals[0].id}/decision",
        json={"decision": "approve", "reviewer": USER, "yaml": yaml},
    )

    assert response.status_code == 422
    assert any(problem in p for p in response.json()["problems"]), response.json()
    assert not (lab.root / "scenarios" / "mined").exists()
    assert len(_records(mined)) == 2


async def test_scenarios_that_do_not_fit_the_project_are_refused(
    client: httpx2.AsyncClient, lab: Lab
) -> None:
    mined = await _mined(client, lab)
    clashing = (await client.get("/api/proposals")).json()[0]["yaml"]
    clashing = clashing.replace("id: mined-double-address", "id: reschedule-visit")
    clashing = clashing.replace("world: acme-starter", "world: elsewhere")

    unknown_persona = await client.post(
        f"/api/proposals/{mined.proposals[1].id}/decision",
        json={"decision": "approve", "reviewer": USER},
    )
    clash = await client.post(
        f"/api/proposals/{mined.proposals[0].id}/decision",
        json={"decision": "approve", "reviewer": USER, "yaml": clashing},
    )

    assert unknown_persona.status_code == 422
    assert unknown_persona.json()["problems"] == [
        "unknown persona(s) nobody-we-know: not in personas/"
    ]
    assert clash.status_code == 422
    assert clash.json()["problems"] == [
        "world 'elsewhere' is not the project's world 'acme-starter'",
        "a scenario with id 'reschedule-visit' already exists",
    ]


async def test_rejecting_records_the_decision_and_writes_no_scenario(
    client: httpx2.AsyncClient, lab: Lab
) -> None:
    mined = await _mined(client, lab)
    proposal = mined.proposals[0]

    response = await client.post(
        f"/api/proposals/{proposal.id}/decision",
        json={"decision": "reject", "reviewer": USER, "note": "duplicate of an existing test"},
    )

    assert response.status_code == 200
    assert response.json()["written"] is None
    decided = _records(mined)[-1]
    assert decided.status is ProposalStatus.REJECTED
    assert decided.note == "duplicate of an existing test"
    assert not (lab.root / "scenarios" / "mined").exists()


async def test_a_decided_proposal_cannot_be_decided_again(
    client: httpx2.AsyncClient, lab: Lab
) -> None:
    mined = await _mined(client, lab)
    url = f"/api/proposals/{mined.proposals[0].id}/decision"
    await client.post(url, json={"decision": "reject", "reviewer": USER})

    again = await client.post(url, json={"decision": "approve", "reviewer": "bob"})

    assert again.status_code == 409
    assert "already rejected by ada" in again.json()["detail"]


async def test_an_existing_scenario_file_is_never_overwritten(
    client: httpx2.AsyncClient, lab: Lab
) -> None:
    mined = await _mined(client, lab)
    target = lab.root / "scenarios" / "mined" / "mined-double-address.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("keep me", encoding="utf-8")

    response = await client.post(
        f"/api/proposals/{mined.proposals[0].id}/decision",
        json={"decision": "approve", "reviewer": USER},
    )

    assert response.status_code == 422
    assert target.read_text(encoding="utf-8") == "keep me"


async def test_unknown_proposal_is_404(client: httpx2.AsyncClient) -> None:
    response = await client.post(
        "/api/proposals/01ARZ3NDEKTSV4RRFFQ69G5FAV/decision",
        json={"decision": "reject", "reviewer": USER},
    )

    assert response.status_code == 404


async def test_a_corrupt_miner_file_is_a_server_error_naming_the_line(
    client: httpx2.AsyncClient, lab: Lab
) -> None:
    mined = await _mined(client, lab)
    with mined.proposals_file.open("a", encoding="utf-8") as out:
        out.write('{"id": "nope"}\n')

    response = await client.get("/api/proposals")

    assert response.status_code == 500
    assert "proposals.jsonl:3: not a ScenarioProposal" in response.json()["detail"]


async def test_us08_an_id_used_under_any_suite_glob_is_refused(lab: Lab) -> None:
    # A suite outside scenarios/ (the acme reference project's `smoke` uses scripted/*.yaml):
    # a mined duplicate of its id would break `resolve_suite` for any suite seeing both files.
    _set_suites(lab, CI_SUITE, 'smoke: { scenarios: "scripted/*.yaml", trials: 1 }')
    starter = lab.root / "scenarios" / "plan-question.yaml"
    scripted = lab.root / "scripted" / "scripted-plan.yaml"
    scripted.parent.mkdir()
    scripted.write_text(
        starter.read_text(encoding="utf-8").replace("id: plan-question", "id: scripted-plan"),
        encoding="utf-8",
    )
    async with _client(lab) as client:
        mined = await _mined(client, lab)
        clashing = (await client.get("/api/proposals")).json()[0]["yaml"]
        clashing = clashing.replace("id: mined-double-address", "id: scripted-plan")

        response = await client.post(
            f"/api/proposals/{mined.proposals[0].id}/decision",
            json={"decision": "approve", "reviewer": USER, "yaml": clashing},
        )

    assert response.status_code == 422
    assert response.json()["problems"] == ["a scenario with id 'scripted-plan' already exists"]
    assert not (lab.root / "scenarios" / "mined").exists()
    assert len(_records(mined)) == 2


async def test_us08_approval_warns_when_no_suite_runs_the_written_scenario(lab: Lab) -> None:
    _set_suites(lab, 'ci: { scenarios: "scenarios/*.yaml", trials: 1 }')
    async with _client(lab) as client:
        mined = await _mined(client, lab)

        response = await client.post(
            f"/api/proposals/{mined.proposals[0].id}/decision",
            json={"decision": "approve", "reviewer": USER},
        )

    assert response.status_code == 200, response.text
    assert response.json()["warnings"] == [
        "no suite runs scenarios/mined/mined-double-address.yaml (ci has the glob "
        "scenarios/*.yaml); add it to a suite in hecklr.yaml"
    ]
    assert (lab.root / "scenarios" / "mined" / "mined-double-address.yaml").is_file()


async def test_approval_into_a_suite_has_no_warning(client: httpx2.AsyncClient, lab: Lab) -> None:
    mined = await _mined(client, lab)

    response = await client.post(
        f"/api/proposals/{mined.proposals[0].id}/decision",
        json={"decision": "approve", "reviewer": USER},
    )

    assert response.json()["warnings"] == []


async def _approve_as_mined(client: httpx2.AsyncClient, lab: Lab) -> dict[str, object]:
    """Approve the first proposal tagged as the miner tags it: ``mined`` and its own tags."""
    mined = await _mined(client, lab)
    text = (await client.get("/api/proposals")).json()[0]["yaml"]
    assert "- core\n- mined\n" in text
    response = await client.post(
        f"/api/proposals/{mined.proposals[0].id}/decision",
        json={
            "decision": "approve",
            "reviewer": USER,
            "yaml": text.replace("- core\n- mined\n", "- mined\n- scheduling\n"),
        },
    )
    assert response.status_code == 200, response.text
    result: dict[str, object] = response.json()
    return result


async def test_issue105_an_approved_mined_scenario_runs_in_the_init_projects_ci_suite(
    client: httpx2.AsyncClient, lab: Lab
) -> None:
    result = await _approve_as_mined(client, lab)

    assert result["warnings"] == []
    suite = resolve_suite(load_run_project(lab.config), "ci")
    assert "mined-double-address" in suite.scenarios


@pytest.mark.parametrize(
    ("suite", "why"),
    [
        (
            'ci: { scenarios: "scenarios/**/*.yaml", tags: [core], trials: 3 }',
            "ci runs only tags [core], the scenario has [mined, scheduling]",
        ),
        (
            'ci: { scenarios: "scenarios/**/*.yaml", trials: 3, channels: [voice] }',
            "ci runs only channels [voice], the scenario is on [chat]",
        ),
    ],
)
async def test_issue105_approval_warns_when_a_suite_filter_drops_the_scenario(
    lab: Lab, suite: str, why: str
) -> None:
    _set_suites(lab, suite)
    async with _client(lab) as client:
        result = await _approve_as_mined(client, lab)

    assert result["warnings"] == [
        f"no suite runs scenarios/mined/mined-double-address.yaml ({why}); add it to a suite in "
        "hecklr.yaml"
    ]
