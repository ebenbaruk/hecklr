"""Voice and email through ``hecklr run``: composition, provider checks and real end-to-end runs.

The end-to-end runs use real transports on 127.0.0.1: the ``websocket`` voice adapter against
``fixtures/voice_agent.py`` (raw PCM, 16 kHz) and the ``webhook`` email adapter against
``fixtures/email_agent.py``, and ``smtp-imap`` against GreenMail in Docker (skipped without
Docker). The customers are scripted and the graders deterministic, so no model is called.
"""

import importlib.util
import re
import socket
import sys
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import click
import pytest
from typer.testing import CliRunner

from hecklr import exit_codes
from hecklr.channels import DEFAULT_ASR, ChannelUnavailableError
from hecklr.cli import app
from hecklr.commands._common import CliOverrides
from hecklr.compose import PreparedRun, RunRequest, execute_run, prepare_run
from hecklr.testing import scripted_models
from hecklr_adapters.email.testing import MailServer, docker_available, greenmail
from hecklr_adapters.voice import WebSocketVoiceAdapterConfig
from hecklr_core import ConfigError, RunResults
from hecklr_schemas import (
    AudioSpanPayload,
    Channel,
    EmailPayload,
    RunStatus,
    UtterancePayload,
)

Edit = Callable[[Path, str, str], None]

pytestmark = [pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]

SCENARIO = """\
id: visit-time
title: Customer asks when the boiler visit is
channels: [voice, email]
persona: polite-regular
world: acme-starter
mode: scripted
goal: Find out when the boiler visit is booked.
user_turns: ["Hi, this is customer C-1001. When is my boiler visit booked?"]
success:
  - type: tool_called
    tool: get_appointment
  - type: contains
    text: booked for
limits: { max_turns: 6, max_duration_s: 60 }
"""
VOICE_PROFILE = "voice: { tts: { provider: synthetic, voice: caller } }\n"
UNREACHABLE_VOICE = "voice: { type: websocket, url: ws://127.0.0.1:9/ }"
"""Enough to compose a voice run; nothing listens there."""


def _fixture(name: str) -> types.ModuleType:
    path = Path(__file__).parent / "fixtures" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"hecklr_fixture_{name}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


voice_agent = _fixture("voice_agent")
email_agent = _fixture("email_agent")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _setup(config_path: Path, edit: Edit, *, voice: str = "", email: str = "") -> None:
    """Give the starter project voice/email adapters, a voice persona and a scripted scenario."""
    adapters = "".join(f"\n    {line}" for line in (voice, email) if line)
    edit(config_path, '"hecklr_agent:reply"}', '"hecklr_agent:reply"}' + adapters)
    edit(config_path, "suites:\n", 'suites:\n  multi: { scenarios: "multi/*.yaml", trials: 1 }\n')
    edit(config_path, "  judge: fake/judge\n", "  judge: fake/judge\n  asr: fake/asr\n")
    persona = config_path.parent / "personas" / "polite-regular.yaml"
    persona.write_text(persona.read_text() + VOICE_PROFILE)
    (config_path.parent / "multi").mkdir()
    (config_path.parent / "multi" / "visit-time.yaml").write_text(SCENARIO)


def _email_webhook(url: str) -> str:
    return f'email: {{ type: webhook, url: "{url}", listen_port: {_free_port()}, timeout_s: 10 }}'


@pytest.fixture
def voice_url() -> Iterator[tuple[str, Any]]:
    with voice_agent.serve_in_thread() as served:
        yield served


@pytest.fixture
def webhook_url() -> Iterator[tuple[str, Any]]:
    with email_agent.serve_webhook() as served:
        yield served


async def _run(config_path: Path, *channels: Channel) -> RunResults:
    request = RunRequest(config_path=config_path, suite="multi", channels=list(channels) or None)
    return await execute_run(prepare_run(request), llm_providers=scripted_models())


def _prepare_voice(config_path: Path) -> PreparedRun:
    return prepare_run(RunRequest(config_path=config_path, suite="multi", channels=[Channel.VOICE]))


# --- configuration ---------------------------------------------------------------------------


def test_rfc0010a_hecklr_yaml_selects_the_websocket_voice_adapter(
    config_path: Path, edit: Edit
) -> None:
    _setup(config_path, edit, voice=UNREACHABLE_VOICE.replace(" }", ", protocol: twilio }"))
    prepared = _prepare_voice(config_path)
    voice: object = prepared.project.config.agent.adapters.voice  # typed by the frozen schema
    assert isinstance(voice, WebSocketVoiceAdapterConfig)
    assert (voice.protocol, voice.wire_rate) == ("twilio", 8_000)
    assert set(prepared.drivers) == {Channel.VOICE}


def test_a_bad_websocket_voice_config_names_its_line(config_path: Path, edit: Edit) -> None:
    _setup(config_path, edit, voice="voice: { type: websocket, url: http://nope }")
    with pytest.raises(ConfigError, match=r"hecklr\.yaml:7.*agent\.adapters\.voice"):
        prepare_run(RunRequest(config_path=config_path, suite="multi"))


def test_voice_without_the_engine_names_the_extra(
    config_path: Path, edit: Edit, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(config_path, edit, voice=UNREACHABLE_VOICE)
    monkeypatch.setitem(sys.modules, "hecklr_voice", None)  # import fails as if not installed
    with pytest.raises(ChannelUnavailableError, match=r"pip install 'hecklr\[voice\]'"):
        _prepare_voice(config_path)


def test_default_asr_without_the_local_extra_names_it(
    config_path: Path, edit: Edit, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(config_path, edit, voice=UNREACHABLE_VOICE)
    edit(config_path, "  asr: fake/asr\n", "")
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a: None if name == "faster_whisper" else real_find_spec(name, *a),
    )
    with pytest.raises(ChannelUnavailableError) as raised:
        _prepare_voice(config_path)
    assert "hecklr-voice[local]" in str(raised.value)
    assert f"models.asr is unset, so the caller hears with {DEFAULT_ASR}" in str(raised.value)


def test_a_cloud_tts_without_its_key_fails_before_the_run(
    config_path: Path, edit: Edit, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(config_path, edit, voice=UNREACHABLE_VOICE)
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
    persona = config_path.parent / "personas" / "polite-regular.yaml"
    edit(persona, "provider: synthetic", "provider: cartesia")
    with pytest.raises(ChannelUnavailableError, match="CARTESIA_API_KEY"):
        _prepare_voice(config_path)


def test_an_unknown_noise_environment_fails_before_the_run(config_path: Path, edit: Edit) -> None:
    _setup(config_path, edit, voice=UNREACHABLE_VOICE)
    persona = config_path.parent / "personas" / "polite-regular.yaml"
    edit(persona, "voice: caller }", "voice: caller }, environment: lava_lamp, snr_db: 10")
    with pytest.raises(ChannelUnavailableError, match="unknown environment 'lava_lamp'"):
        _prepare_voice(config_path)


# --- end to end ------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.slow
@pytest.mark.sc4_timing
async def test_ws_wire_voice_run_measures_latency_records_audio_and_reaches_the_world(
    config_path: Path, edit: Edit, voice_url: tuple[str, Any]
) -> None:
    url, log = voice_url
    _setup(config_path, edit, voice=f"voice: {{ type: websocket, url: {url} }}")

    results = await _run(config_path, Channel.VOICE)

    assert results.run.status is RunStatus.COMPLETED
    [trial] = results.trials
    assert (trial.channel, trial.status) == (Channel.VOICE, "ok"), trial.error
    assert all(grade.passed for grade in results.grades if grade.criterion_index is not None)
    # The agent found its trial's world from the start message, not a fixed address.
    [start] = log.starts
    assert start["world_url"].endswith(f"/t/{trial.id}")
    assert log.lookups == [200]
    events = [e for e in results.events if e.trial_id == trial.id]
    replies = [
        e.payload for e in events if e.actor == "agent" and isinstance(e.payload, UtterancePayload)
    ]
    assert replies[0].text.startswith("your boiler visit is booked for")
    latency = replies[0].latency_ms
    assert latency is not None
    assert abs(latency - voice_agent.REPLY_AFTER_MS) <= 20  # SC4 over a real socket
    recordings = {e.payload.recording for e in events if isinstance(e.payload, AudioSpanPayload)}
    run_dir = config_path.parent / ".hecklr" / "runs" / results.run.id
    assert recordings
    assert all(r is not None and (run_dir / r).is_file() for r in recordings)


@pytest.mark.anyio
async def test_ws_wire_email_webhook_run_threads_and_reaches_the_world(
    config_path: Path, edit: Edit, webhook_url: tuple[str, Any]
) -> None:
    url, log = webhook_url
    _setup(config_path, edit, email=_email_webhook(url))

    results = await _run(config_path, Channel.EMAIL)

    [trial] = results.trials
    assert (trial.channel, trial.status) == (Channel.EMAIL, "ok"), trial.error
    assert all(grade.passed for grade in results.grades if grade.criterion_index is not None)
    assert log.world_urls == [f"{log.world_urls[0].rpartition('/t/')[0]}/t/{trial.id}"]
    emails = [
        (e.actor, e.payload.message) for e in results.events if isinstance(e.payload, EmailPayload)
    ]
    customer = [message for actor, message in emails if actor == "user"]
    agent = [message for actor, message in emails if actor == "agent"]
    assert agent[0].in_reply_to == customer[0].message_id
    assert agent[0].body.startswith("Hello,\n\nYour boiler visit is booked for")


@pytest.mark.anyio
async def test_two_webhook_runs_in_one_process_reuse_the_reply_port(
    config_path: Path, edit: Edit, webhook_url: tuple[str, Any]
) -> None:
    # The first run must stop its reply endpoint, or the second cannot bind the same port.
    _setup(config_path, edit, email=_email_webhook(webhook_url[0]))
    for _ in range(2):
        results = await _run(config_path, Channel.EMAIL)
        assert [t.status for t in results.trials] == ["ok"]


@pytest.fixture(scope="module")
def mail_server() -> Iterator[MailServer]:
    if not docker_available():
        pytest.skip("Docker is not available for the GreenMail mail server")
    with greenmail() as server:
        yield server


@pytest.mark.anyio
@pytest.mark.slow
async def test_ws_wire_email_smtp_imap_run_through_greenmail(
    config_path: Path, edit: Edit, mail_server: MailServer
) -> None:
    email = (
        f"email: {{ type: smtp-imap, inbox: {email_agent.AGENT_ADDR}, "
        f"smtp_host: 127.0.0.1, smtp_port: {mail_server.smtp_port}, "
        f"imap_host: 127.0.0.1, imap_port: {mail_server.imap_port}, reply_timeout_s: 30 }}"
    )
    _setup(config_path, edit, email=email)

    with email_agent.serve_mailbox(mail_server) as log:
        results = await _run(config_path, Channel.EMAIL)

    [trial] = results.trials
    assert trial.status == "ok", trial.error
    assert all(grade.passed for grade in results.grades if grade.criterion_index is not None)
    assert log.world_urls[0].endswith(f"/t/{trial.id}")  # from the X-Hecklr-World-Url header


def test_ws_wire_hecklr_run_channels_voice_email_from_the_cli(
    config_path: Path, edit: Edit, voice_url: tuple[str, Any], webhook_url: tuple[str, Any]
) -> None:
    _setup(
        config_path,
        edit,
        voice=f"voice: {{ type: websocket, url: {voice_url[0]} }}",
        email=_email_webhook(webhook_url[0]),
    )
    result = CliRunner().invoke(
        app,
        ["run", "-c", str(config_path), "-s", "multi", "--channels", "voice,email"],
        obj=CliOverrides(scripted_models()),
    )
    output = click.unstyle(result.output)
    assert result.exit_code == exit_codes.OK, output
    assert "Suite multi: 2 trials over 1 scenarios" in output
    run_id = re.search(r"Run ([0-9A-Z]{26}) \(", output)
    assert run_id is not None
    run_dir = config_path.parent / ".hecklr" / "runs" / run_id.group(1)
    report = (run_dir / "report.html").read_text()
    # The voice trial's recordings play in the report (embedded while small, else linked).
    assert report.count('<audio controls preload="none"') == 2  # the caller's and the agent's
    assert "response_latency" in report
