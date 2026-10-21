"""The email channel: ``EmailDriver`` implements ``hecklr_schemas.ChannelDriver`` for email.

Build it from ``hecklr.yaml`` with ``make_email_driver(agent.adapters.email)``, or around any
``EmailTarget`` (e.g. ``hecklr_adapters.email.PythonEmailTarget``) with ``EmailDriver``.
"""

from typing import Final

from hecklr_adapters.email import make_email_target
from hecklr_channels.email.driver import DEFAULT_CUSTOMER_DOMAIN, EmailDriver
from hecklr_channels.email.quoting import (
    QUOTE_STYLES,
    QuoteStyle,
    persona_quote_style,
    quote,
    visible_text,
)
from hecklr_schemas import EmailAdapterConfig, SmtpImapAdapterConfig, WebhookAdapterConfig

WEBHOOK_AGENT_ADDR: Final = "agent@hecklr.test"
"""The ``To`` of customer emails over the webhook adapter, whose config names no inbox."""


def make_email_driver(config: EmailAdapterConfig) -> EmailDriver:
    """The email driver for an ``agent.adapters.email`` entry, with its deadline and addresses.

    Raises:
        AdapterConfigError: the adapter configuration cannot work.
    """
    target = make_email_target(config)
    match config:
        case SmtpImapAdapterConfig():
            return EmailDriver(
                target,
                agent_addr=config.inbox,
                customer_domain=config.from_domain,
                reply_timeout_s=config.reply_timeout_s,
            )
        case WebhookAdapterConfig():
            return EmailDriver(
                target, agent_addr=WEBHOOK_AGENT_ADDR, reply_timeout_s=config.timeout_s
            )


__all__ = [
    "DEFAULT_CUSTOMER_DOMAIN",
    "QUOTE_STYLES",
    "WEBHOOK_AGENT_ADDR",
    "EmailDriver",
    "QuoteStyle",
    "make_email_driver",
    "persona_quote_style",
    "quote",
    "visible_text",
]
