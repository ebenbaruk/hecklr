"""``hecklr replay FILE`` with a scripted extraction model."""

import json
from pathlib import Path

import click
from pydantic import JsonValue
from typer.testing import CliRunner

from hecklr import exit_codes
from hecklr.cli import app
from hecklr.commands._common import CliOverrides
from hecklr_core import load_model
from hecklr_llm import FakeLLM, LLMRequest
from hecklr_schemas import Scenario, SimulationMode

runner = CliRunner()

CONVERSATIONS: list[dict[str, JsonValue]] = [
    {
        "id": "prod-1",
        "turns": [
            {"role": "user", "text": "Hi, I am C-1001 and I need to move my boiler visit."},
            {"role": "agent", "text": "Done, it is now on Monday."},
        ],
    },
    {
        "id": "prod-2",
        "turns": [{"role": "user", "text": "hello?"}, {"role": "agent", "text": "Hi!"}],
    },
]


def _extractor(request: LLMRequest) -> dict[str, JsonValue]:
    if "prod-2" in request.messages[-1].content:
        return {"title": "", "goal": " ", "facts": [], "resolving_tool": None, "tags": []}
    return {
        "title": "Customer moves a boiler visit",
        "goal": "Move the boiler visit.",
        "facts": [{"name": "customer_id", "value": "C-1001"}],
        "resolving_tool": None,
        "tags": ["scheduling"],
    }


def _replay(config_path: Path, transcripts: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(
        app,
        ["replay", str(transcripts), "--config", str(config_path), *args],
        obj=CliOverrides({"fake": FakeLLM(responder=_extractor)}),
    )
    return result.exit_code, click.unstyle(result.output)


def _transcripts(tmp_path: Path, conversations: list[dict[str, JsonValue]]) -> Path:
    path = tmp_path / "logs.jsonl"
    path.write_text("\n".join(json.dumps(c) for c in conversations) + "\n", encoding="utf-8")
    return path


def test_us02_replay_writes_scenarios_and_reports_skipped_conversations(
    config_path: Path, tmp_path: Path
) -> None:
    code, output = _replay(config_path, _transcripts(tmp_path, CONVERSATIONS))

    assert code == exit_codes.CHECK_FAILED
    assert "skipped prod-2" in output
    assert "1 replay scenario(s) written, 1 skipped" in output
    written = config_path.parent / "scenarios" / "replay" / "replay-prod-1.yaml"
    scenario = load_model(written, Scenario)
    assert scenario.mode is SimulationMode.REPLAY
    assert scenario.world == "acme-starter"
    assert scenario.persona == "busy-commuter"  # the first persona of the library, by file name
    assert scenario.facts == {"customer_id": "C-1001"}


def test_replay_honours_out_and_persona(config_path: Path, tmp_path: Path) -> None:
    out = tmp_path / "replays"

    code, output = _replay(
        config_path,
        _transcripts(tmp_path, CONVERSATIONS[:1]),
        "--out",
        str(out),
        "--persona",
        "polite-regular",
    )

    assert code == exit_codes.OK, output
    assert load_model(out / "replay-prod-1.yaml", Scenario).persona == "polite-regular"


def test_replay_never_overwrites_and_rejects_invalid_files(
    config_path: Path, tmp_path: Path
) -> None:
    transcripts = _transcripts(tmp_path, CONVERSATIONS[:1])
    _replay(config_path, transcripts)

    code, output = _replay(config_path, transcripts)
    assert code == exit_codes.CONFIG_ERROR
    assert "refusing to overwrite" in output

    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"id": "x"}\n', encoding="utf-8")
    code, output = _replay(config_path, bad)
    assert code == exit_codes.CONFIG_ERROR
    assert "not a valid conversation" in output


def test_replay_needs_a_persona(config_path: Path, tmp_path: Path) -> None:
    for persona in (config_path.parent / "personas").glob("*.yaml"):
        persona.unlink()

    code, output = _replay(config_path, _transcripts(tmp_path, CONVERSATIONS[:1]))

    assert code == exit_codes.CONFIG_ERROR
    assert "pass --persona" in output


ATTACKER = """\
id: data-prober
description: Tries to get another customer's address.
adversarial: cross_customer_data_probe
"""


def test_issue63_replay_skips_adversarial_personas_by_default(
    config_path: Path, tmp_path: Path
) -> None:
    # Sorted by path, personas/adversarial/ comes before every ordinary persona.
    attackers = config_path.parent / "personas" / "adversarial"
    attackers.mkdir()
    (attackers / "data-prober.yaml").write_text(ATTACKER, encoding="utf-8")

    code, output = _replay(config_path, _transcripts(tmp_path, CONVERSATIONS[:1]))

    assert code == exit_codes.OK, output
    written = config_path.parent / "scenarios" / "replay" / "replay-prod-1.yaml"
    assert load_model(written, Scenario).persona == "busy-commuter"


def test_issue63_replay_with_only_adversarial_personas_needs_a_persona(
    config_path: Path, tmp_path: Path
) -> None:
    personas = config_path.parent / "personas"
    for persona in personas.glob("*.yaml"):
        persona.unlink()
    (personas / "data-prober.yaml").write_text(ATTACKER, encoding="utf-8")
    transcripts = _transcripts(tmp_path, CONVERSATIONS[:1])

    code, output = _replay(config_path, transcripts)
    assert code == exit_codes.CONFIG_ERROR
    assert "no non-adversarial persona" in output

    code, output = _replay(config_path, transcripts, "--persona", "data-prober")
    assert code == exit_codes.OK, output
