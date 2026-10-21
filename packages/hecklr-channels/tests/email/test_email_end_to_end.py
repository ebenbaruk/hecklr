"""Email conversations end to end over real transports, graded for threading.

- SMTP/IMAP: a GreenMail server in Docker and a tiny mailbox agent that reads its inbox over IMAP
  and answers over SMTP (skipped when Docker is unavailable).
- Webhook: an inbound-parse style agent on 127.0.0.1 that POSTs its replies back.

Each runs a clean agent (threading passes) and an F11 agent that drops ``In-Reply-To`` and
``References`` (threading fails).
"""

import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import anyio
import httpx2
import pytest
import uvicorn
from fastapi import FastAPI, Request, Response

from hecklr_adapters.email import SmtpImapEmailTarget, WebhookEmailTarget
from hecklr_adapters.email.testing import MailServer, run_mailbox_agent
from hecklr_channels import EmailDriver
from hecklr_channels.email import WEBHOOK_AGENT_ADDR
from hecklr_schemas import EmailMessage, EndReason, MonotonicClock, WebhookAdapterConfig

from ..fakes import ListSink, ScriptedCustomer
from .support import broken_reply, email_brief, email_context, emails, grade_threading
from .support import threaded_reply as clean_reply

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.e2e,
    pytest.mark.enable_socket,
    pytest.mark.allow_hosts(["127.0.0.1"]),
]

LINES = ["My boiler visit on Thursday needs moving.", "Tuesday morning works."]
REPLY = {"clean": clean_reply, "f11": broken_reply}


@pytest.mark.slow
@pytest.mark.parametrize(("agent", "threaded"), [("clean", True), ("f11", False)])
async def test_ws12_email_over_smtp_imap_is_threaded_and_graded(
    mail_server: MailServer, agent: str, threaded: bool
) -> None:
    inbox = f"support.{agent}@acme.test"  # one inbox per agent, so the two runs never mix

    def answer(msg: EmailMessage) -> EmailMessage:
        return REPLY[agent](msg).model_copy(update={"from_addr": inbox})

    target = SmtpImapEmailTarget(mail_server.config(inbox), poll_interval_s=0.1)
    driver = EmailDriver(target, agent_addr=inbox, reply_timeout_s=30)
    sink = ListSink(MonotonicClock())
    brief = email_brief()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(run_mailbox_agent, mail_server, inbox, answer)
        result = await driver.run(email_context(), brief, ScriptedCustomer(LINES), sink)
        tasks.cancel_scope.cancel()

    assert (result.status, result.end_reason, result.turns) == ("ok", EndReason.GOAL_ACHIEVED, 4)
    assert sink.kinds() == ["user:email", "agent:email"] * 2
    [(sent, first), (_, second)] = emails(sink.events, "user")
    [(received, reply), _] = emails(sink.events, "agent")
    assert received.t_ms > sent.t_ms  # reply time on the harness clock
    assert reply.to_addrs == [first.from_addr]
    assert second.in_reply_to == reply.message_id  # the customer always threads
    grade = await grade_threading(sink.events, brief)
    assert (grade.status, grade.passed) == ("ok", threaded), grade.rationale


class WebhookAgent:
    """An inbound-parse style agent: answers each POSTed email by POSTing to ``reply_url``."""

    def __init__(self, reply: Callable[[EmailMessage], EmailMessage]) -> None:
        self.reply = reply

    def app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/inbound")
        async def inbound(request: Request) -> Response:
            post = await request.json()
            reply = self.reply(EmailMessage.model_validate(post["message"]))
            async with httpx2.AsyncClient() as client:
                answer = await client.post(post["reply_url"], json=reply.model_dump(mode="json"))
            return Response(status_code=200 if answer.status_code == 202 else 502)

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


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.mark.parametrize(("agent", "threaded"), [("clean", True), ("f11", False)])
async def test_ws12_email_over_webhook_is_threaded_and_graded(agent: str, threaded: bool) -> None:
    with serve(WebhookAgent(REPLY[agent]).app()) as base:
        config = WebhookAdapterConfig.model_validate(
            {
                "type": "webhook",
                "url": f"{base}/inbound",
                "listen_port": free_port(),
                "timeout_s": 5,
            }
        )
        target = WebhookEmailTarget(config)
        driver = EmailDriver(target, agent_addr=WEBHOOK_AGENT_ADDR, reply_timeout_s=5)
        sink = ListSink(MonotonicClock())
        brief = email_brief()
        try:
            result = await driver.run(email_context(), brief, ScriptedCustomer(LINES), sink)
        finally:
            target.close()

    assert (result.status, result.turns) == ("ok", 4), result.error
    assert sink.kinds() == ["user:email", "agent:email"] * 2
    grade = await grade_threading(sink.events, brief)
    assert (grade.status, grade.passed) == ("ok", threaded), grade.rationale
