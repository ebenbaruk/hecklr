"""The voice agent's WebSocket shell: the ``pcm`` protocol, ``/healthz`` and its world."""

import contextlib
import json
from collections.abc import AsyncIterator

import anyio
import httpx2
import numpy as np
import pytest
from acme_agent import AcmeAgent, AgentSettings, load_agent_files
from acme_voice import SpeechStack, VoiceAgentServer, bound_port, running_server
from voice_testing import ScriptedASR, config
from voice_trials import WORLD
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from hecklr_adapters import WORLD_URL_HEADER
from hecklr_schemas import TRIAL_ID_HEADER
from hecklr_voice import EnergyVad, SyntheticTTS

pytestmark = [pytest.mark.anyio, pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]

START = {"type": "start", "sample_rate": 16_000, "encoding": "pcm_s16le"}


class World:
    """A stand-in world sandbox over HTTP: records which URLs the agent's tools called."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.urls.append(str(request.url))
        return httpx2.Response(200, json={"customer_id": "C-1188", "verified": True})


@contextlib.asynccontextmanager
async def agent(
    world: World, *, base_url: str | None = None
) -> AsyncIterator[tuple[str, VoiceAgentServer]]:
    stack = SpeechStack(
        asr=ScriptedASR(["I need to cancel my visit, I am C-1188 at postcode 75005."]),
        tts=SyntheticTTS(),
        vad=EnergyVad,
        voice="agent",
    )
    rules = AcmeAgent(load_agent_files(), AgentSettings(brain="rules"))
    transport = httpx2.MockTransport(world.handler)
    async with httpx2.AsyncClient(transport=transport) as client:
        server = VoiceAgentServer(rules, WORLD, config(), stack, client, world_base_url=base_url)
        async with running_server(server) as running:
            yield f"ws://127.0.0.1:{bound_port(running)}/", server


async def until_closed(socket: ClientConnection) -> None:
    with contextlib.suppress(ConnectionClosed):
        while True:
            await socket.recv()


async def say_customer_id(url: str, start: dict[str, object], headers: dict[str, str]) -> None:
    """Open a call, say one sentence (the ears hear the scripted line), wait for the answer."""
    async with connect(url, additional_headers=headers) as socket:
        await socket.send(json.dumps(start))
        assert isinstance(await socket.recv(), bytes)  # the greeting
        speech = (np.sin(np.arange(16_000) / 3) * 8000).astype("<i2")
        await socket.send(speech.tobytes() + bytes(2 * 16_000))
        with anyio.fail_after(10):
            while not isinstance(await socket.recv(), bytes):  # the answer
                pass
        await socket.send(json.dumps({"type": "stop"}))
        await until_closed(socket)


@pytest.mark.parametrize(
    ("start", "headers", "base_url", "source", "url"),
    [
        (
            {"world_url": "http://w/t/1", "trial_id": "1"},
            {WORLD_URL_HEADER: "http://h/t/1"},
            "http://b",
            "start",
            "http://w/t/1/tools/",
        ),
        ({}, {WORLD_URL_HEADER: "http://h/t/1"}, "http://b", "header", "http://h/t/1/tools/"),
        ({}, {TRIAL_ID_HEADER: "T9"}, "http://b/", "base_url", "http://b/t/T9/tools/"),
        ({"trial_id": "T8"}, {}, "http://b", "base_url", "http://b/t/T8/tools/"),
        ({}, {}, "http://b", "local", None),
    ],
)
async def test_tools_go_to_the_trials_world(
    start: dict[str, object],
    headers: dict[str, str],
    base_url: str,
    source: str,
    url: str | None,
) -> None:
    world = World()
    async with agent(world, base_url=base_url) as (address, server):
        await say_customer_id(address, {**START, **start}, headers)
        async with httpx2.AsyncClient() as client:
            health = (await client.get(address.replace("ws://", "http://") + "healthz")).json()
    assert health["calls_by_world_source"] == {source: 1}
    assert health["calls_total"] == 1
    assert health["variant"] == "clean"
    assert health["brain"] == "rules"
    if url is None:
        assert world.urls == []
    else:
        assert world.urls
        assert all(called.startswith(url) for called in world.urls)
    assert server.calls[0].heard == ["I need to cancel my visit, I am C-1188 at postcode 75005."]


@pytest.mark.parametrize(
    "messages",
    [
        (b"\x00\x00",),
        (json.dumps({"type": "start", "sample_rate": 8000}),),
        ("not json",),
        ("[1, 2]",),
        (json.dumps(START), b"\x00"),
    ],
    ids=["audio-before-start", "wrong-rate", "malformed-json", "not-an-object", "odd-bytes"],
)
async def test_protocol_violations_close_the_call_with_1003(
    messages: tuple[str | bytes, ...],
) -> None:
    async with agent(World()) as (address, _), connect(address) as socket:
        for message in messages:
            await socket.send(message)
        await until_closed(socket)
        assert socket.close_code == 1003
