"""ChatDriver over the real adapters on the real clock: measured latency matches injected delay.

The agent stamps when it got the message and when it emitted its first and last token, on the
same monotonic clock. Comparing against stamps, not the nominal sleeps, keeps the test exact
under parallel load, where a sleep can overshoot.

In process (``python`` adapter) the agent's stamps are the reference: nothing sits between the
agent and the driver. Over HTTP the customer also waits for the request and each token to cross
the socket and for the server thread to be scheduled, which under a loaded machine takes tens of
milliseconds the agent's stamps cannot see (issue #15). There the reference is taken at the
client side of the transport (request handed to the socket, token bytes received); the driver
must match it within the SC4 tolerance (+-20 ms), and it can never be faster than the agent.
"""

import asyncio
import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

import httpx2
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from hecklr_adapters import AgentSession, HttpChatTarget
from hecklr_adapters.python import PythonChatTarget
from hecklr_channels import ChatDriver
from hecklr_schemas import (
    TRIAL_ID_HEADER,
    AgentChunk,
    EndReason,
    Event,
    HttpAdapterConfig,
    MonotonicClock,
    ToolCallPayload,
    UtterancePayload,
)

from .fakes import ListSink, ScriptedCustomer, brief, context

pytestmark = pytest.mark.anyio

FIRST_TOKEN_S = 0.2
TOKEN_GAP_S = 0.05
TOKENS = ["Your ", "visit ", "is ", "moved."]
TOLERANCE_MS = 20


class DelayedTokens:
    """Streams ``TOKENS`` after injected delays, stamping what really happened."""

    def __init__(self) -> None:
        self.stamps: list[float] = []

    async def stream(self) -> AsyncIterator[str]:
        self.stamps.append(time.monotonic())
        await asyncio.sleep(FIRST_TOKEN_S)
        for index, token in enumerate(TOKENS):
            if index:
                await asyncio.sleep(TOKEN_GAP_S)
            self.stamps.append(time.monotonic())
            yield token

    def agent_ttft_ttlt_ms(self) -> tuple[float, float]:
        """Time to first and last token as the agent saw it, checking the delay was injected."""
        start, first, last = self.stamps[0], self.stamps[1], self.stamps[-1]
        ttft_ms = (first - start) * 1000
        assert ttft_ms >= FIRST_TOKEN_S * 1000
        return ttft_ms, (last - start) * 1000


def assert_measured(event: Event, reply: UtterancePayload, ttft_ms: float, ttlt_ms: float) -> None:
    """The driver's time to first and last token match the reference within the tolerance."""
    assert reply.latency_ms is not None
    assert reply.end_ms is not None
    assert abs(reply.latency_ms - ttft_ms) <= TOLERANCE_MS
    assert abs(reply.latency_ms + reply.end_ms - event.t_ms - ttlt_ms) <= TOLERANCE_MS


class StampedStream(httpx2.AsyncByteStream):
    """A response body that stamps when each token's bytes have fully arrived."""

    def __init__(self, inner: httpx2.AsyncByteStream, arrivals: dict[str, float]) -> None:
        self._inner = inner
        self._arrivals = arrivals

    async def __aiter__(self) -> AsyncIterator[bytes]:
        received = b""
        async for chunk in self._inner:
            received += chunk
            now = time.monotonic()
            for token in TOKENS:
                if token not in self._arrivals and json.dumps(token).encode() in received:
                    self._arrivals[token] = now
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class StampingTransport(httpx2.AsyncHTTPTransport):
    """The real transport, stamping the request's send and each token's arrival client-side."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: float | None = None
        self.arrivals: dict[str, float] = {}

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.sent = time.monotonic()
        response = await super().handle_async_request(request)
        assert isinstance(response.stream, httpx2.AsyncByteStream)
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=StampedStream(response.stream, self.arrivals),
            extensions=response.extensions,
        )

    def ttft_ttlt_ms(self) -> tuple[float, float]:
        """Time to first and last token as the client received them."""
        assert self.sent is not None
        first, last = self.arrivals[TOKENS[0]], self.arrivals[TOKENS[-1]]
        return (first - self.sent) * 1000, (last - self.sent) * 1000


async def test_ws6_chat_python_adapter_latency_matches_injected_delay() -> None:
    delayed = DelayedTokens()

    async def agent(session: AgentSession, message: str) -> AsyncIterator[str | AgentChunk]:
        yield AgentChunk(tool_call=ToolCallPayload(call_id="c1", tool="reschedule"))
        async for token in delayed.stream():
            yield token

    sink = ListSink(MonotonicClock())
    result = await ChatDriver(PythonChatTarget(agent)).run(
        context(), brief(), ScriptedCustomer(["move my visit"]), sink
    )

    assert (result.status, result.end_reason, result.turns) == ("ok", EndReason.GOAL_ACHIEVED, 2)
    assert sink.kinds() == ["user:utterance", "agent:tool_call", "agent:utterance"]
    [(event, reply)] = sink.utterances("agent")
    assert reply.text == "".join(TOKENS)
    assert_measured(event, reply, *delayed.agent_ttft_ttlt_ms())


@contextmanager
def _serve(app: FastAPI) -> Iterator[int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        yield sock.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
async def test_ws6_chat_http_sse_adapter_latency_matches_injected_delay() -> None:
    app = FastAPI()
    delayed = DelayedTokens()
    trial_ids: list[str | None] = []

    @app.post("/chat")
    async def chat(request: Request) -> StreamingResponse:
        trial_ids.append(request.headers.get(TRIAL_ID_HEADER))

        async def events() -> AsyncIterator[str]:
            async for token in delayed.stream():
                yield f"data: {json.dumps({'delta': token})}\n\n"
            yield "event: end\ndata:\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    transport = StampingTransport()
    with _serve(app) as port:
        config = HttpAdapterConfig.model_validate(
            {
                "type": "http",
                "url": f"http://127.0.0.1:{port}/chat",
                "stream": "sse",
                "response_path": "delta",
            }
        )
        sink = ListSink(MonotonicClock())
        ctx = context()
        result = await ChatDriver(HttpChatTarget(config, transport=transport)).run(
            ctx, brief(), ScriptedCustomer(["move my visit", "never sent"]), sink
        )

    assert (result.status, result.end_reason, result.turns) == ("ok", EndReason.AGENT_ENDED, 2)
    assert trial_ids == [ctx.trial_id]
    [(event, reply)] = sink.utterances("agent")
    assert reply.text == "".join(TOKENS)
    agent_ttft_ms, agent_ttlt_ms = delayed.agent_ttft_ttlt_ms()
    client_ttft_ms, client_ttlt_ms = transport.ttft_ttlt_ms()
    # The client cannot receive a token before the agent emitted it (1 ms: stamp rounding).
    assert client_ttft_ms >= agent_ttft_ms - 1
    assert client_ttlt_ms >= agent_ttlt_ms - 1
    assert_measured(event, reply, client_ttft_ms, client_ttlt_ms)
