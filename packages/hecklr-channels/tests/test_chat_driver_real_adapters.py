"""ChatDriver over the real adapters on the real clock: measured latency matches injected delay.

The agent stamps when it got the message and when it emitted its first and last token, on the
same monotonic clock; the driver's time to first and last token must match within the SC4
tolerance (+-20 ms). Comparing against those stamps, not the nominal sleeps, keeps the test exact
under parallel load, where a sleep can overshoot.
"""

import asyncio
import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import TypeAdapter

from hecklr_adapters import AgentSession, make_chat_target
from hecklr_adapters.python import PythonChatTarget
from hecklr_channels import ChatDriver
from hecklr_schemas import (
    TRIAL_ID_HEADER,
    AgentChunk,
    ChatAdapterConfig,
    EndReason,
    Event,
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

    def assert_measured(self, event: Event, reply: UtterancePayload) -> None:
        assert reply.latency_ms is not None
        assert reply.end_ms is not None
        start, first, last = self.stamps[0], self.stamps[1], self.stamps[-1]
        true_ttft_ms = (first - start) * 1000
        true_ttlt_ms = (last - start) * 1000
        assert true_ttft_ms >= FIRST_TOKEN_S * 1000  # the delay really was injected
        assert abs(reply.latency_ms - true_ttft_ms) <= TOLERANCE_MS
        assert abs(reply.latency_ms + reply.end_ms - event.t_ms - true_ttlt_ms) <= TOLERANCE_MS


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
    delayed.assert_measured(event, reply)


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

    with _serve(app) as port:
        config = TypeAdapter[ChatAdapterConfig](ChatAdapterConfig).validate_python(
            {
                "type": "http",
                "url": f"http://127.0.0.1:{port}/chat",
                "stream": "sse",
                "response_path": "delta",
            }
        )
        sink = ListSink(MonotonicClock())
        ctx = context()
        result = await ChatDriver(make_chat_target(config)).run(
            ctx, brief(), ScriptedCustomer(["move my visit", "never sent"]), sink
        )

    assert (result.status, result.end_reason, result.turns) == ("ok", EndReason.AGENT_ENDED, 2)
    assert trial_ids == [ctx.trial_id]
    [(event, reply)] = sink.utterances("agent")
    assert reply.text == "".join(TOKENS)
    delayed.assert_measured(event, reply)
