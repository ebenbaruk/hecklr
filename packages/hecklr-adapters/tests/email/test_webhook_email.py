"""The webhook email adapter against a real inbound-parse style agent on 127.0.0.1."""

import http.client
import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import anyio
import httpx2
import pytest
import uvicorn
from fastapi import FastAPI, Request, Response

from hecklr_adapters import AgentError, make_email_target
from hecklr_adapters.email import REPLY_PATH, WebhookEmailTarget, make_reply
from hecklr_schemas import (
    TRIAL_ID_HEADER,
    EmailMessage,
    EmailTarget,
    SessionContext,
    TrialId,
    WebhookAdapterConfig,
    new_id,
)

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.enable_socket,
    pytest.mark.allow_hosts(["127.0.0.1"]),
]

AGENT = "support@acme.test"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class InboundAgent:
    """Answers each inbound email by POSTing a reply to ``reply_url``, SendGrid-parse style."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.trial_headers: list[str | None] = []
        self.reply_statuses: list[int] = []

    def app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/inbound")
        async def inbound(request: Request) -> Response:
            post = await request.json()
            self.posts.append(post)
            self.trial_headers.append(request.headers.get(TRIAL_ID_HEADER))
            message = EmailMessage.model_validate(post["message"])
            if message.body == "/error":
                return Response("boom", status_code=500)
            reply = make_reply(message, body=f"Re-{message.body}", from_addr=AGENT)
            async with httpx2.AsyncClient() as client:
                answer = await client.post(post["reply_url"], json=reply.model_dump(mode="json"))
            self.reply_statuses.append(answer.status_code)
            return Response(status_code=200)

        return app


@contextmanager
def serve(app: FastAPI) -> Iterator[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        assert time.monotonic() < deadline, "agent server did not start"
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def webhook_config(url: str, **extra: object) -> WebhookAdapterConfig:
    data = {"type": "webhook", "url": url, "listen_port": free_port(), "timeout_s": 5} | extra
    return WebhookAdapterConfig.model_validate(data)


@pytest.fixture
def agent() -> InboundAgent:
    return InboundAgent()


@pytest.fixture
def target(agent: InboundAgent) -> Iterator[WebhookEmailTarget]:
    with serve(agent.app()) as base:
        built = make_email_target(webhook_config(f"{base}/inbound", headers={"X-Key": "k"}))
        assert isinstance(built, WebhookEmailTarget)
        yield built
        built.close()


def email(body: str = "Move my visit", sender: str = "c.1@customers.hecklr.test") -> EmailMessage:
    return EmailMessage(
        message_id=f"<{new_id()}@customers.hecklr.test>", subject="Visit", body=body,
        from_addr=sender, to_addrs=[AGENT],
    )  # fmt: skip


def ctx() -> SessionContext:
    return SessionContext(trial_id=TrialId(new_id()), seed=1, timeout_s=5)


async def test_ws12_webhook_round_trip_routes_the_reply_to_its_trial(
    target: WebhookEmailTarget, agent: InboundAgent
) -> None:
    assert isinstance(target, EmailTarget)
    context, other = ctx(), ctx()
    sent = email()
    replies = target.replies(context)
    await target.deliver(sent, context)
    with anyio.fail_after(5):
        reply = await anext(replies)
    assert (reply.in_reply_to, reply.body) == (sent.message_id, "Re-Move my visit")
    [post] = agent.posts
    assert post["trial_id"] == context.trial_id
    assert post["reply_url"] == target.reply_url
    assert post["reply_url"].endswith(REPLY_PATH)
    assert agent.trial_headers == [context.trial_id]
    assert agent.reply_statuses == [202]
    other_replies = target.replies(other)
    with anyio.move_on_after(0.2) as scope:
        await anext(other_replies)
    assert scope.cancelled_caught  # nothing leaked to another trial
    await replies.aclose()  # type: ignore[attr-defined]
    await other_replies.aclose()  # type: ignore[attr-defined]


async def test_ws12_webhook_agent_http_error_is_an_agent_error(target: WebhookEmailTarget) -> None:
    with pytest.raises(AgentError, match="HTTP 500: boom"):
        await target.deliver(email("/error"), ctx())


async def test_ws12_webhook_unreachable_agent_is_an_agent_error() -> None:
    target = WebhookEmailTarget(webhook_config(f"http://127.0.0.1:{free_port()}/inbound"))
    try:
        with pytest.raises(AgentError, match="failed"):
            await target.deliver(email(), ctx())
    finally:
        target.close()


async def test_ws12_webhook_stalled_agent_is_a_timeout() -> None:
    async def stall(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("stalled", request=request)

    config = webhook_config("http://agent.test/inbound")
    target = WebhookEmailTarget(config, transport=httpx2.MockTransport(stall))
    try:
        with pytest.raises(AgentError, match=r"sent nothing for 5\.0 s"):
            await target.deliver(email(), ctx())
    finally:
        target.close()


def post_raw(target: WebhookEmailTarget, path: str, body: bytes, length: int | None = None) -> int:
    port = int(target.reply_url.split(":")[2].split("/")[0])
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Length": str(len(body) if length is None else length)}
    conn.request("POST", path, body=body, headers=headers)
    status = conn.getresponse().status
    conn.close()
    return status


async def test_ws12_webhook_reply_endpoint_rejects_what_it_cannot_route(
    target: WebhookEmailTarget,
) -> None:
    target.start()
    target.start()  # idempotent
    stray = make_reply(email(sender="nobody@else.test"), body="?", from_addr=AGENT)
    payload = json.dumps(stray.model_dump(mode="json")).encode()
    assert post_raw(target, REPLY_PATH, payload) == 404
    assert post_raw(target, REPLY_PATH, b'{"subject": 1}') == 400
    assert post_raw(target, "/elsewhere", payload) == 404
    assert post_raw(target, REPLY_PATH, b"x", length=100 * 1024 * 1024) == 413


async def test_issue119_a_webhook_reply_with_a_lone_surrogate_is_refused_to_the_agent(
    target: WebhookEmailTarget,
) -> None:
    target.start()
    reply = make_reply(email(), body="Sure", from_addr=AGENT)
    payload = json.dumps(reply.model_dump(mode="json")).replace('"Sure"', '"Sure \\ud83d"')
    # Refused before it is routed: the agent hears why, and nothing unstorable is queued.
    assert post_raw(target, REPLY_PATH, payload.encode()) == 400


async def test_ws12_webhook_close_without_start_is_harmless() -> None:
    WebhookEmailTarget(webhook_config("http://agent.test/inbound")).close()
