"""``hecklr mine LOGS`` (US-08) on the starter project with scripted models."""

import json
import re
from pathlib import Path
from typing import Any

import click
import pytest
from typer.testing import CliRunner

from hecklr import exit_codes
from hecklr.cli import app
from hecklr.commands._common import CliOverrides
from hecklr_core import load_model, load_project
from hecklr_fix import FixError, load_mined_target
from hecklr_llm import FakeLLM, LLMRequest
from hecklr_mine import MINE_DIR, decide, generate_log_set, load_queue, read_summary
from hecklr_schemas import FailureCluster, ProposalStatus, Scenario

runner = CliRunner()


def _reply(request: LLMRequest) -> dict[str, Any]:
    prompt = request.messages[-1].content
    if request.schema_name == "ClusterLabel":
        first = re.search(r"Customer: (\w+ \w+ \w+ \w+)", prompt)
        return {"label": f"fails when {first.group(1) if first else 'x'}", "description": "d"}
    if request.schema_name == "ReplayExtraction":
        customer = re.search(r"^CUSTOMER: (.*)$", prompt, re.MULTILINE)
        goal = customer.group(1) if customer else "help"
        return {"title": f"Customer: {goal[:50]}", "goal": goal, "facts": [],
                "resolving_tool": None, "tags": ["support"]}  # fmt: skip
    if request.schema_name == "JudgeVerdict":
        return {"verdict": "pass", "rationale": "fine", "evidence": []}
    if request.schema_name == "FailureSummary":
        return {"summary": "The agent failed the customer."}
    if request.schema_name == "FoundEntities":
        return {"entities": []}
    raise AssertionError(request.schema_name)


def _mine(config_path: Path, logs: Path, *args: str, llm: FakeLLM | None = None) -> tuple[int, str]:
    result = runner.invoke(
        app,
        ["mine", str(logs), "--config", str(config_path), *args],
        obj=CliOverrides({"fake": llm or FakeLLM(responder=_reply)}),
    )
    return result.exit_code, click.unstyle(result.output)


@pytest.fixture
def logs(tmp_path: Path) -> Path:
    path = tmp_path / "production.jsonl"
    generate_log_set(300, seed=4).write_jsonl(path)
    return path


def test_us08_mine_writes_clusters_and_queues_proposals(config_path: Path, logs: Path) -> None:
    code, output = _mine(config_path, logs, "--judge", "resolved")

    assert code == exit_codes.OK, output
    assert "Mined 300 conversation(s)" in output
    assert "scenario proposal(s) queued for review" in output
    mine_root = config_path.parent / ".hecklr" / MINE_DIR
    [run_dir] = [path for path in mine_root.iterdir() if path.is_dir()]
    summary = read_summary(run_dir)
    assert summary.conversations == 300
    assert summary.clusters >= 5
    assert summary.model == "fake/customer"
    assert f"{summary.clusters} cluster(s)" in output
    clusters = [
        FailureCluster.model_validate_json(line)
        for line in (run_dir / "clusters.jsonl").read_text().splitlines()
    ]
    assert all(cluster.label in output for cluster in clusters)
    queue = load_queue(mine_root)
    assert len(queue) == summary.proposals > 0
    for proposal in queue:
        scenario = load_model(run_dir / "scenarios" / f"{proposal.scenario.id}.yaml", Scenario)
        assert scenario.world == "acme-starter"
        assert scenario.persona == "busy-commuter"

    # A second run over the same logs proposes nothing new: everything is already queued.
    code, output = _mine(config_path, logs)
    assert code == exit_codes.OK, output
    assert "duplicate" in output
    assert len(load_queue(mine_root)) == len(queue)


def test_us10_hecklr_fix_targets_what_hecklr_mine_wrote(config_path: Path, logs: Path) -> None:
    """``hecklr mine``'s real output (synthetic logs, offline embedder, FakeLLM) is what
    ``hecklr fix CLUSTER`` loads: the cluster as mined, with its unrejected proposals."""
    code, output = _mine(config_path, logs)
    assert code == exit_codes.OK, output
    project = load_project(config_path)
    mine_root = project.output_dir / MINE_DIR
    [run_dir] = [path for path in mine_root.iterdir() if path.is_dir()]
    clusters = [
        FailureCluster.model_validate_json(line)
        for line in (run_dir / "clusters.jsonl").read_text().splitlines()
    ]
    queue = load_queue(mine_root)
    proposed = [cluster for cluster in clusters if any(p.cluster_id == cluster.id for p in queue)]
    assert proposed, output
    rejected = next(p for p in queue if p.cluster_id == proposed[0].id)
    decide(mine_root, rejected.id, ProposalStatus.REJECTED, reviewer="ops@acme")

    for cluster in proposed:
        expected = [p.scenario for p in queue if p.cluster_id == cluster.id and p.id != rejected.id]
        if not expected:
            with pytest.raises(FixError, match="no proposed or approved scenario"):
                load_mined_target(mine_root, cluster.id)
            continue
        target = load_mined_target(mine_root, cluster.id)
        assert target.cluster == cluster
        assert list(target.scenarios) == expected
        assert all(scenario.world == "acme-starter" for scenario in target.scenarios)


def test_mine_without_a_model_clusters_offline(
    config_path: Path, logs: Path, tmp_path: Path
) -> None:
    out = tmp_path / "mined"
    code, output = _mine(config_path, logs, "--no-model", "--out", str(out))

    assert code == exit_codes.OK, output
    assert "failures about" in output
    assert read_summary(out).proposals == 0
    assert "0 scenario proposal(s)" in output

    code, output = _mine(config_path, logs, "--no-model", "--out", str(out))
    assert code == exit_codes.CONFIG_ERROR
    assert "already holds a mining run" in output


def test_mine_reads_other_formats_with_a_mapping(config_path: Path, tmp_path: Path) -> None:
    logs = tmp_path / "logs.csv"
    logs.write_text("conv,who,msg\nc1,customer,hello\nc1,bot,hi\n", encoding="utf-8")
    mapping = tmp_path / "mapping.yaml"
    mapping.write_text("conversation_id: conv\nrole: who\ntext: msg\n", encoding="utf-8")

    code, output = _mine(
        config_path, logs, "--format", "csv", "--mapping", str(mapping), "--no-model"
    )

    assert code == exit_codes.OK, output
    assert "Mined 1 conversation(s): 0 failed (none)" in output


@pytest.mark.parametrize(
    ("args", "content", "message"),
    [
        ((), "not json\n", "not a valid conversation"),
        (("--format", "otel"), json.dumps({"a": 1}), "not OTLP JSON"),
        (("--no-model", "--llm-summaries"), None, "need a model"),
        (("--embedder", "openai", "--no-model"), None, "needs an API key in OPENAI_API_KEY"),
        (("--presidio",), None, "spacy download"),
        (("--persona", "nobody"), None, "no persona 'nobody'"),
    ],
)
def test_mine_reports_input_problems_as_config_errors(
    config_path: Path,
    logs: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
    content: str | None,
    message: str,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if content is not None:
        logs.write_text(content, encoding="utf-8")
    monkeypatch.setattr("importlib.util.find_spec", lambda name, *a: None)

    code, output = _mine(config_path, logs, *args)

    assert code == exit_codes.CONFIG_ERROR
    assert re.search(message, output), output


def test_mine_stops_at_the_budget(config_path: Path, logs: Path) -> None:
    code, output = _mine(config_path, logs, "--budget-usd", "0")

    assert code == exit_codes.BUDGET
    assert "reached the cap" in output


@pytest.mark.parametrize("args", [("--judge", "resolved"), ("--llm-scrub",), ("--llm-summaries",)])
def test_mine_stops_at_the_budget_inside_concurrent_stages(
    config_path: Path, logs: Path, args: tuple[str, ...]
) -> None:
    code, output = _mine(config_path, logs, *args, "--budget-usd", "0")

    assert code == exit_codes.BUDGET, output
    assert "reached the cap" in output
    assert "ExceptionGroup" not in output


@pytest.mark.parametrize("args", [("--judge", "resolved"), ("--llm-scrub",), ("--llm-summaries",)])
def test_mine_reports_model_failures_as_harness_errors(
    config_path: Path, logs: Path, args: tuple[str, ...]
) -> None:
    code, output = _mine(config_path, logs, *args, llm=FakeLLM(script=[]))

    assert code == exit_codes.HARNESS_ERROR, output
    assert "model call failed" in output


def test_mine_keeps_going_when_summary_and_scrub_replies_are_unusable(
    config_path: Path, logs: Path, tmp_path: Path
) -> None:
    def unusable(request: LLMRequest) -> Any:
        if request.schema_name in {"FailureSummary", "FoundEntities"}:
            return "not json"
        return _reply(request)

    out = tmp_path / "mined"
    code, output = _mine(
        config_path,
        logs,
        "--llm-scrub",
        "--llm-summaries",
        "--out",
        str(out),
        llm=FakeLLM(responder=unusable),
    )

    assert code == exit_codes.OK, output
    summary = read_summary(out)
    assert len(summary.scrub_errors) == 300
    assert len(summary.summary_errors) == summary.failures > 0
    assert "model replies unusable" in output
    assert "person names are kept" not in output


def test_mine_warns_that_default_scrubbing_keeps_names(config_path: Path, logs: Path) -> None:
    code, output = _mine(config_path, logs, "--no-model")

    assert code == exit_codes.OK, output
    assert "person names are kept" in output


def test_mine_without_a_persona_library_needs_persona(config_path: Path, logs: Path) -> None:
    for persona in (config_path.parent / "personas").glob("*.yaml"):
        persona.unlink()

    code, output = _mine(config_path, logs, "--no-model")

    assert code == exit_codes.CONFIG_ERROR
    assert "no non-adversarial persona" in output


def test_mine_builds_the_openai_embedder_from_the_environment(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    logs = tmp_path / "logs.csv"
    logs.write_text("conversation_id,role,text\nc1,user,hi\nc1,agent,hello\n", encoding="utf-8")

    # No failure, so nothing is embedded and no request leaves the machine.
    code, output = _mine(config_path, logs, "--format", "csv", "--embedder", "openai", "--no-model")

    assert code == exit_codes.OK, output
    assert "0 failed" in output


def test_mine_default_persona_skips_adversarial_ones(
    config_path: Path, logs: Path, tmp_path: Path
) -> None:
    library = config_path.parent / "personas"
    (library / "adversarial").mkdir()
    (library / "adversarial" / "aaa-attacker.yaml").write_text(
        "id: aaa-attacker\ndescription: Tries refund fraud\nadversarial: refund_fraud\n",
        encoding="utf-8",
    )

    code, output = _mine(config_path, logs)

    assert code == exit_codes.OK, output
    queue = load_queue(config_path.parent / ".hecklr" / MINE_DIR)
    assert queue
    assert {proposal.scenario.persona for proposal in queue} == {"busy-commuter"}
