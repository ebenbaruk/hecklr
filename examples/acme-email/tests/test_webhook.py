"""The agent behind its webhook, driven by Hecklr's own ``webhook`` adapter and email driver.

The agent's tools act on a real ``hecklr_world.Sandbox`` over HTTP, as in ``hecklr run``. Servers
run on 127.0.0.1 ephemeral ports: these tests get localhost sockets only.
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

import anyio
import httpx2
import pytest
from acme_email import create_app
from email_testing import (
    EmailTrial,
    email_desk,
    free_port,
    load_scenarios,
    run_served_email_trial,
    serving,
)

from hecklr_adapters import WORLD_URL_HEADER
from hecklr_adapters.email import WebhookEmailTarget
from hecklr_channels import EmailDriver
from hecklr_channels.email import WEBHOOK_AGENT_ADDR
from hecklr_schemas import (
    TRIAL_ID_HEADER,
    EmailMessage,
    Persona,
    Scenario,
    TrialId,
    WebhookAdapterConfig,
    World,
    new_id,
)
from hecklr_world import Sandbox

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.enable_socket,
    pytest.mark.allow_hosts(["127.0.0.1"]),
]

SCENARIOS = {s.id: s for s in load_scenarios("scripted/*.yaml")}
REPLY_TIMEOUT_S = 3.0


@contextmanager
def webhook_target(agent_url: str, **headers: str) -> Iterator[WebhookEmailTarget]:
    config = WebhookAdapterConfig.model_validate(
        {
            "type": "webhook",
            "url": f"{agent_url}/email",
            "listen_port": free_port(),
            "headers": headers,
            "timeout_s": REPLY_TIMEOUT_S,
        }
    )
    target = WebhookEmailTarget(config)
    try:
        yield target
    finally:
        target.close()


@asynccontextmanager
async def agent_on_sandbox(variant: str, world: World) -> AsyncIterator[tuple[str, Sandbox]]:
    """The agent's webhook server, its tools on a running sandbox (``ACME_WORLD_BASE_URL``)."""
    async with Sandbox() as sandbox:
        app = create_app(email_desk(variant), world, world_base_url=sandbox.base_url)
        async with serving(app) as url:
            yield url, sandbox


async def webhook_trial(
    variant: str, scenario: Scenario, personas: dict[str, Persona], world: World
) -> tuple[EmailTrial, dict[str, object]]:
    async with agent_on_sandbox(variant, world) as (url, sandbox):
        with webhook_target(url) as target:
            driver = EmailDriver(
                target, agent_addr=WEBHOOK_AGENT_ADDR, reply_timeout_s=REPLY_TIMEOUT_S
            )
            persona = personas[scenario.persona_ids[0]]
            trial = await run_served_email_trial(scenario, persona, world, sandbox, driver)
        async with httpx2.AsyncClient() as client:
            health = (await client.get(f"{url}/healthz")).json()
    return trial, health


@pytest.mark.parametrize(
    ("variant", "scenario_id", "failed"),
    [
        ("clean", "email-cancel-1", []),
        ("clean", "email-timeout-emergency-1", []),
        ("F11", "email-cancel-1", ["email_threading"]),
        ("F12", "email-cancel-1", []),
        ("F12", "email-timeout-emergency-1", ["timeout", "regex"]),
    ],
)
async def test_sc8_webhook_agent_on_a_real_sandbox(
    variant: str,
    scenario_id: str,
    failed: list[str],
    personas: dict[str, Persona],
    world: World,
) -> None:
    trial, health = await webhook_trial(variant, SCENARIOS[scenario_id], personas, world)
    assert trial.failed() == failed
    assert health["variant"] == variant
    sources = health["requests_by_world_source"]
    assert isinstance(sources, dict)
    assert sources["base_url"] >= 1
    assert sources["local"] == 0
    if variant == "F12" and failed:
        assert trial.emails("agent") == []  # dead air: the email was never answered


async def test_the_world_url_header_beats_the_base_url(
    personas: dict[str, Persona], world: World
) -> None:
    # What Hecklr's webhook adapter sends once it forwards the trial's world (ADR 0007d).
    scenario = SCENARIOS["email-cancel-1"]
    async with Sandbox() as sandbox:
        app = create_app(email_desk(), world, world_base_url="http://127.0.0.1:9")
        async with serving(app) as url:
            persona = personas[scenario.persona_ids[0]]
            # The header names the world before the trial opens it: fix the trial id up front.
            trial_id = TrialId(new_id())
            world_url = f"{sandbox.base_url}/t/{trial_id}"
            with webhook_target(url, **{WORLD_URL_HEADER: world_url}) as target:
                driver = EmailDriver(
                    target, agent_addr=WEBHOOK_AGENT_ADDR, reply_timeout_s=REPLY_TIMEOUT_S
                )
                trial = await run_served_email_trial(
                    scenario, persona, world, sandbox, driver, trial_id=trial_id
                )
            async with httpx2.AsyncClient() as client:
                health = (await client.get(f"{url}/healthz")).json()
    assert trial.passed, trial.failed()
    assert health["requests_by_world_source"]["header"] == 3


async def test_a_reply_that_cannot_be_delivered_is_counted_and_logged(world: World) -> None:
    refused: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        refused.append(request)
        return httpx2.Response(404, text="no live conversation")

    app = create_app(email_desk(), world, transport=httpx2.MockTransport(handler))
    email = EmailMessage(
        message_id="<1@customers.hecklr.test>",
        subject="Visit",
        body="I need to cancel my visit",
        from_addr="c@customers.hecklr.test",
        to_addrs=["agent@hecklr.test"],
    )
    async with serving(app) as url, httpx2.AsyncClient() as client:
        posted = await client.post(
            f"{url}/email",
            json={"reply_url": "http://hecklr.test/replies", "message": email.model_dump()},
            headers={TRIAL_ID_HEADER: "T1"},
        )
        assert posted.status_code == 202
        with anyio.fail_after(5):
            while (health := (await client.get(f"{url}/healthz")).json())["replies_failed"] < 1:  # noqa: ASYNC110 -- only HTTP exposes it
                await anyio.sleep(0.02)
    assert health["replies_sent"] == 0
    assert health["in_flight"] == 0
    assert health["requests_by_world_source"]["local"] == 1
    assert [str(request.url) for request in refused] == ["http://hecklr.test/replies"]
    assert EmailMessage.model_validate_json(refused[0].content).in_reply_to == email.message_id


async def test_a_malformed_inbound_email_is_rejected(world: World) -> None:
    async with serving(create_app(email_desk(), world)) as url, httpx2.AsyncClient() as client:
        posted = await client.post(f"{url}/email", json={"reply_url": "x", "message": {}})
    assert posted.status_code == 422
