"""Build the email adapter for an ``agent.adapters.email`` entry of ``hecklr.yaml``."""

from hecklr_adapters.email.smtp_imap import SmtpImapEmailTarget
from hecklr_adapters.email.webhook import WebhookEmailTarget
from hecklr_schemas import (
    EmailAdapterConfig,
    EmailTarget,
    SmtpImapAdapterConfig,
    WebhookAdapterConfig,
)


def make_email_target(config: EmailAdapterConfig) -> EmailTarget:
    """Return the ``EmailTarget`` for ``config``, validated so it fails before any trial.

    Raises:
        AdapterConfigError: the configuration cannot work (e.g. a credential variable is unset).
    """
    match config:
        case SmtpImapAdapterConfig():
            return SmtpImapEmailTarget(config)
        case WebhookAdapterConfig():
            return WebhookEmailTarget(config)
