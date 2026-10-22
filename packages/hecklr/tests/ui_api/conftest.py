"""Fixtures for the UI API: a starter project with real runs, a voice run and miner files.

The two chat runs are produced by ``hecklr run`` on scripted ``fake/`` models (offline), so the
API is tested on the exact files a real run leaves: ``run.json``, Parquet exports, DuckDB. The
candidate's judges fail every "Saturdays" conversation, so the comparison has a flip.
"""

import io
import re
import shutil
import sys
import wave
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import click
import httpx2
import numpy as np
import pytest
from fastapi import FastAPI
from pydantic import BaseModel
from typer.testing import CliRunner

from hecklr.cli import app as cli_app
from hecklr.commands._common import CliOverrides
from hecklr.compose import load_run_project
from hecklr.scaffold import AgentChoice, ModelChoice, write_project
from hecklr.testing import JudgeRule, scripted_models
from hecklr.ui_api import create_app
from hecklr_core import ResultStore
from hecklr_schemas import (
    Actor,
    AgentRef,
    AudioSpanPayload,
    Budget,
    Channel,
    ClusterId,
    Conversation,
    ConversationTurn,
    Event,
    EventId,
    EventPayload,
    FailureCluster,
    InterruptPayload,
    LLMRequest,
    ProposalId,
    Run,
    RunId,
    RunStatus,
    Scenario,
    ScenarioProposal,
    Trial,
    TrialId,
    UtterancePayload,
    WorldSnapshot,
    new_id,
)

FAKE_MODELS = ModelChoice("fake/customer", "fake/judge", "tests")
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
USER = "ada"


@dataclass(frozen=True)
class Lab:
    """A project with stored runs: ``baseline`` all-pass, ``candidate`` with a flipped scenario."""

    root: Path
    baseline: str
    candidate: str

    @property
    def config(self) -> Path:
        return self.root / "hecklr.yaml"

    @property
    def output_dir(self) -> Path:
        return self.root / ".hecklr"


def _run(config: Path, judge_rule: JudgeRule | None = None) -> str:
    result = CliRunner().invoke(
        cli_app,
        ["run", "--config", str(config), "--trials", "2"],
        obj=CliOverrides(scripted_models(judge_rule)),
    )
    output = click.unstyle(result.output)
    assert result.exit_code == 0, output
    match = re.search(r"Run ([0-9A-Z]{26}) \(", output)
    assert match, output
    return match.group(1)


def _no_saturdays(request: LLMRequest) -> bool:
    return "Saturdays" not in request.messages[-1].content


@pytest.fixture(scope="module")
def lab_template(tmp_path_factory: pytest.TempPathFactory) -> Lab:
    """Two real runs, made once per module (each takes a couple of seconds)."""
    root = tmp_path_factory.mktemp("lab") / "shop"
    write_project(root, "shop-agent", AgentChoice("python"), FAKE_MODELS)
    with pytest.MonkeyPatch.context() as patch:
        # The run imports the project's starter agent; keep sys.path and modules as they were.
        patch.setattr(sys, "path", list(sys.path))
        patch.setattr(sys, "modules", dict(sys.modules))
        config = root / "hecklr.yaml"
        baseline = _run(config)
        candidate = _run(config, _no_saturdays)
    return Lab(root=root, baseline=baseline, candidate=candidate)


@pytest.fixture
def lab(lab_template: Lab, tmp_path: Path) -> Lab:
    """A private copy of the lab, so tests that write labels or scenarios stay independent."""
    root = tmp_path / "shop"
    shutil.copytree(lab_template.root, root)
    return Lab(root=root, baseline=lab_template.baseline, candidate=lab_template.candidate)


def make_app(lab: Lab, static_dir: Path | None = None) -> FastAPI:
    project = load_run_project(lab.config)
    return create_app(project, user=USER, static_dir=static_dir, clock=Clock())


class Clock:
    """A wall clock that advances one second per reading, so labels have distinct times."""

    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
async def client(lab: Lab) -> AsyncIterator[httpx2.AsyncClient]:
    """The API of ``lab`` over ASGI (no socket)."""
    transport = httpx2.ASGITransport(app=make_app(lab))
    async with httpx2.AsyncClient(transport=transport, base_url="http://ui") as http:
        yield http


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- a voice run with a recording -------------------------------------------------------------

SAMPLE_RATE = 8_000


def wav_bytes(seconds: float) -> bytes:
    """A 16-bit mono tone, the format the voice engine records."""
    samples = np.arange(int(SAMPLE_RATE * seconds))
    tone = (0.3 * np.sin(2 * np.pi * 440 * samples / SAMPLE_RATE) * 32767).astype("<i2")
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(tone.tobytes())
    return out.getvalue()


async def store_voice_run(lab: Lab) -> tuple[str, str]:
    """A finished voice run with one trial, its mix recording and one escaping recording path."""
    run = Run(
        id=RunId(new_id()),
        created_at=NOW,
        agent=AgentRef(name="shop-agent"),
        config_hash="0" * 64,
        seed=7,
        suite="voice",
        matrix=[],
        budget=Budget(usd=Decimal(1)),
        status=RunStatus.COMPLETED,
    )
    trial_id = TrialId(new_id())
    world = WorldSnapshot(world_id="acme-starter", taken_at_ms=0, collections={})
    trial = Trial(
        id=trial_id,
        run_id=run.id,
        scenario_id="check-appointment",
        persona_id="busy-commuter",
        channel=Channel.VOICE,
        index=0,
        seed=1,
        status="ok",
        started_at=NOW,
        duration_ms=3_000,
        cost_usd=Decimal(0),
        world_before=world,
        world_after=world,
    )
    recording = f"recordings/{trial_id}/mix.wav"
    payloads: list[tuple[Actor, int, EventPayload]] = [
        ("user", 500, AudioSpanPayload(start_ms=500, end_ms=1500, recording=recording,
                                       start_sample=4000, sample_rate=SAMPLE_RATE)),
        ("user", 500, UtterancePayload(text="When is my visit?", turn=0, end_ms=1500)),
        ("agent", 1900, AudioSpanPayload(start_ms=1900, end_ms=2800, recording=recording)),
        ("agent", 1900, UtterancePayload(text="Tuesday at nine.", turn=1, end_ms=2800,
                                         latency_ms=400)),
        ("user", 2500, InterruptPayload(target="agent", reaction_ms=120)),
        ("user", 2900, AudioSpanPayload(start_ms=2900, end_ms=2950,
                                        recording="../../../secret.wav")),
    ]  # fmt: skip
    events = [
        Event(id=EventId(new_id()), trial_id=trial_id, seq=seq, t_ms=t, actor=actor, payload=p)
        for seq, (actor, t, p) in enumerate(payloads)
    ]

    async with ResultStore(lab.output_dir / "runs") as store:
        await store.write_run(run)
        await store.append_trial_result(trial, events, ())
        await store.export(run.id)
    path = lab.output_dir / "runs" / run.id / recording
    path.parent.mkdir(parents=True)
    path.write_bytes(wav_bytes(3.0))
    return run.id, trial_id


# --- miner output -----------------------------------------------------------------------------


def scenario(scenario_id: str = "mined-double-address", persona: str = "busy-commuter") -> Scenario:
    return Scenario.model_validate(
        {
            "id": scenario_id,
            "title": "Customer gives two addresses",
            "channels": ["chat"],
            "tags": ["core", "mined"],
            "persona": persona,
            "world": "acme-starter",
            "goal": "Book a visit while mentioning both your home and office addresses.",
            "facts": {"customer_id": "C-1002"},
            "success": [{"type": "judge", "rubric": "resolved"}],
            "source": {"kind": "mined", "conversation_id": "prod-1"},
        }
    )


@dataclass(frozen=True)
class Mined:
    run_cluster: FailureCluster
    production_cluster: FailureCluster
    proposals: list[ScenarioProposal]
    proposals_file: Path


def write_mine(lab: Lab, member_trials: list[str]) -> Mined:
    """Miner files as ADR 0016a specifies them, in a dated subdirectory."""
    directory = lab.output_dir / "mine" / "2026-10-22"
    directory.mkdir(parents=True)
    run_cluster = FailureCluster(
        id=ClusterId(new_id()),
        label="fails on Saturday questions",
        source="run",
        trial_ids=[TrialId(t) for t in member_trials],
        exemplar_trial_ids=[TrialId(member_trials[0])],
        created_at=NOW,
    )
    production = FailureCluster(
        id=ClusterId(new_id()),
        label="fails when the customer gives two addresses",
        description="The agent books the office address.",
        source="production",
        conversation_ids=["prod-1", "prod-2"],
        exemplar_conversation_ids=["prod-1"],
        created_at=NOW + timedelta(minutes=1),
    )
    proposals = [
        ScenarioProposal(
            id=ProposalId(new_id()),
            cluster_id=production.id,
            scenario=scenario(),
            created_at=NOW,
        ),
        ScenarioProposal(
            id=ProposalId(new_id()),
            cluster_id=production.id,
            scenario=scenario("mined-second", persona="nobody-we-know"),
            created_at=NOW + timedelta(seconds=1),
        ),
    ]
    conversation = Conversation(
        id="prod-1",
        turns=[
            ConversationTurn(role="user", text="Book me at home, not 3 rue de la Paix."),
            ConversationTurn(role="agent", text="Booked at 3 rue de la Paix."),
        ],
    )
    _jsonl(directory / "clusters.jsonl", [run_cluster, production])
    _jsonl(directory / "conversations.jsonl", [conversation])
    proposals_file = directory / "proposals.jsonl"
    _jsonl(proposals_file, proposals)
    return Mined(run_cluster, production, proposals, proposals_file)


def _jsonl(path: Path, records: Sequence[BaseModel]) -> None:
    path.write_text("".join(r.model_dump_json() + "\n" for r in records), encoding="utf-8")
