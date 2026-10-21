"""Quoting styles, reading replies back, ``make_email_driver``, and a FakeLLM customer by email."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hecklr_adapters import AdapterConfigError
from hecklr_adapters.email import PythonEmailTarget, SmtpImapEmailTarget, WebhookEmailTarget
from hecklr_channels import EmailDriver, make_email_driver
from hecklr_channels.email import (
    QUOTE_STYLES,
    WEBHOOK_AGENT_ADDR,
    persona_quote_style,
    quote,
    visible_text,
)
from hecklr_llm import FakeLLM
from hecklr_schemas import (
    EmailMessage,
    EndReason,
    FakeClock,
    LLMProvider,
    SmtpImapAdapterConfig,
    WebhookAdapterConfig,
)
from hecklr_sim import make_simulator
from hecklr_sim.testing import decision_reply

from ..fakes import ListSink
from .support import AGENT, ThreadedAgent, email_brief, email_context, emails, grade_threading

PARENT = EmailMessage(
    message_id="<a1@acme.test>",
    subject="Re: Move a visit",
    body="Which day suits you?\n\nBest,\nAcme",
    from_addr=AGENT,
    to_addrs=["c@customers.hecklr.test"],
)


def test_ws12_quote_styles_place_the_new_text_where_each_client_does() -> None:
    top = quote("Tuesday", PARENT, "top_post")
    bottom = quote("Tuesday", PARENT, "bottom_post")
    assert top.startswith("Tuesday\n\nOn a previous message, support@acme.test wrote:\n")
    assert bottom.endswith("> Acme\n\nTuesday")
    assert "> Which day suits you?\n>\n> Best," in top
    assert quote("Tuesday", PARENT, "none") == "Tuesday"


@pytest.mark.parametrize("style", QUOTE_STYLES)
def test_ws12_the_agent_side_reading_of_a_quoted_reply_is_the_new_text(style: str) -> None:
    assert visible_text(quote("Tuesday works.", PARENT, style)) == "Tuesday works."  # type: ignore[arg-type]


def test_ws12_visible_text_drops_forwarded_history_and_attributions() -> None:
    body = "Done.\nOn Mon, 3 Nov 2026, Bob wrote:\n> old\n-----Original Message-----\nFrom: x\nold"
    assert visible_text(body) == "Done."


@given(st.text(alphabet=st.characters(blacklist_characters=">\r"), max_size=80))
def test_ws12_unquoted_text_survives_visible_text(text: str) -> None:
    lines = [line for line in text.split("\n") if not line.lstrip().startswith(("On ", "-"))]
    plain = "\n".join(lines)
    assert visible_text(plain) == plain.strip()


def test_ws12_a_persona_keeps_one_quote_style_and_personas_differ() -> None:
    styles = {persona_quote_style(f"persona-{i}") for i in range(40)}
    assert styles == set(QUOTE_STYLES)
    assert persona_quote_style("camille") == persona_quote_style("camille")


def test_ws12_make_email_driver_binds_smtp_imap_settings() -> None:
    config = SmtpImapAdapterConfig(
        type="smtp-imap", inbox="support@acme.test", from_domain="c.test", reply_timeout_s=30
    )
    driver = make_email_driver(config)
    assert isinstance(driver, EmailDriver)
    assert isinstance(driver._settings.target, SmtpImapEmailTarget)
    assert (driver._settings.agent_addr, driver._settings.customer_domain) == (AGENT, "c.test")
    assert driver._settings.reply_timeout_s == 30


def test_ws12_make_email_driver_binds_the_webhook_adapter() -> None:
    config = WebhookAdapterConfig.model_validate(
        {"type": "webhook", "url": "http://agent.test/inbound", "timeout_s": 9}
    )
    driver = make_email_driver(config)
    assert isinstance(driver._settings.target, WebhookEmailTarget)
    assert (driver._settings.agent_addr, driver._settings.reply_timeout_s) == (
        WEBHOOK_AGENT_ADDR,
        9,
    )


def test_ws12_make_email_driver_fails_before_any_trial_on_bad_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MAIL_USER", raising=False)
    config = SmtpImapAdapterConfig(
        type="smtp", inbox=AGENT, username_env="MAIL_USER", password_env="MAIL_PASS"
    )
    with pytest.raises(AdapterConfigError, match="MAIL_USER is not set"):
        make_email_driver(config)


@pytest.mark.anyio
@pytest.mark.fake
async def test_ws12_email_end_to_end_with_a_fake_llm_customer() -> None:
    llm: LLMProvider = FakeLLM(
        script=[
            decision_reply("Hi, my boiler visit on Thursday needs moving.", subject="Boiler visit"),
            decision_reply("Tuesday morning would be perfect."),
            decision_reply("Thanks!", action="end", end_reason="goal_achieved"),
        ]
    )
    brief = email_brief()
    simulator = make_simulator("fake/sim", clock_factory=FakeClock)(brief, llm)
    sink = ListSink(FakeClock())
    driver = EmailDriver(PythonEmailTarget(ThreadedAgent()), agent_addr=AGENT)

    result = await driver.run(email_context(), brief, simulator, sink)

    assert (result.status, result.end_reason, result.turns) == ("ok", EndReason.GOAL_ACHIEVED, 5)
    [(_, first), (_, second), (_, goodbye)] = emails(sink.events, "user")
    assert first.subject == "Boiler visit"  # the simulator's own subject line
    assert second.subject == goodbye.subject == "Re: Boiler visit"
    assert visible_text(goodbye.body) == "Thanks!"  # sent as the closing email, unanswered
    grade = await grade_threading(sink.events, brief)
    assert grade.passed, grade.rationale
