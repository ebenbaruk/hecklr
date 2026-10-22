"""The loopback server: the ``pcm`` protocol, its health report, its CLI, and one real call."""

import contextlib
import json
from collections.abc import AsyncIterator

import httpx2
import numpy as np
import pytest
from loopback_agent import LoopbackServer, LoopbackSettings, bound_port, running_server
from loopback_agent.__main__ import main, parse_args, settings_from
from loopback_agent.bench import agent_arguments, place_call
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from hecklr_voice.audio import FRAME_SAMPLES

pytestmark = [pytest.mark.anyio, pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]

START = json.dumps({"type": "start", "sample_rate": 16_000, "encoding": "pcm_s16le"})


@pytest.fixture
async def url() -> AsyncIterator[str]:
    loopback = LoopbackServer(LoopbackSettings(reply_after_ms=300.0, greeting=True))
    async with running_server(loopback) as server:
        yield f"ws://127.0.0.1:{bound_port(server)}/"


async def until_closed(socket: ClientConnection) -> None:
    """Read until the agent closes the call."""
    with contextlib.suppress(ConnectionClosed):
        while True:
            await socket.recv()


async def close_code(url: str, *messages: str | bytes) -> int | None:
    async with connect(url) as socket:
        for message in messages:
            await socket.send(message)
        await until_closed(socket)
        return socket.close_code


@pytest.mark.parametrize(
    "messages",
    [
        (b"\x00\x00",),
        (json.dumps({"type": "start", "sample_rate": 8000}),),
        ("not json",),
        (START, b"\x00"),
    ],
    ids=["audio-before-start", "wrong-rate", "malformed-json", "odd-byte-count"],
)
async def test_protocol_violations_close_the_call_with_1003(
    url: str, messages: tuple[str | bytes, ...]
) -> None:
    assert await close_code(url, *messages) == 1003


async def test_greeting_arrives_as_pcm_and_stop_ends_the_call(url: str) -> None:
    async with connect(url) as socket:
        await socket.send(START)
        await socket.send(json.dumps({"type": "ping"}))  # unknown control messages are ignored
        first = await socket.recv()
        assert isinstance(first, bytes)
        assert len(first) == 2 * FRAME_SAMPLES
        assert np.abs(np.frombuffer(first, dtype="<i2")).max() > 0
        await socket.send(json.dumps({"type": "stop"}))
        await until_closed(socket)
        assert socket.close_code == 1000


async def test_health_reports_settings_calls_and_cpu(url: str) -> None:
    await close_code(url, b"\x00\x00")
    async with httpx2.AsyncClient() as client:
        response = await client.get(url.replace("ws://", "http://") + "healthz")
    body = response.json()
    assert body["status"] == "ok"
    assert body["settings"]["reply_after_ms"] == 300.0
    assert body["stats"]["calls_total"] == 1
    assert body["stats"]["calls_active"] == 0
    assert body["cpu_s"] > 0


async def test_a_real_hecklr_call_measures_the_injected_delay(url: str) -> None:
    measured = await place_call(url, seed=3, barge_in=False, lines=["Hello, I need help."])
    assert measured.result.status == "ok"
    # Agent and harness share this test's event loop here, so the bound is loose; SC4 proper
    # runs the agent in its own process (test_sc4_network.py).
    assert measured.latencies_ms
    assert all(abs(latency - 300) <= 50 for latency in measured.latencies_ms)


def test_too_fast_a_barge_in_reaction_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 100 ms"):
        LoopbackServer(LoopbackSettings(barge_in_reaction_ms=50.0))


def test_cli_options_round_trip_through_the_bench_arguments() -> None:
    settings = LoopbackSettings(
        reply_after_ms=900.0, barge_in_reaction_ms=None, endpoint_ms=150.0, greeting=True
    )
    assert settings_from(parse_args(agent_arguments(settings))) == settings


def test_cli_rejects_invalid_settings(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--barge-in-reaction-ms=20"]) == 2
    assert "at least 100 ms" in capsys.readouterr().err
