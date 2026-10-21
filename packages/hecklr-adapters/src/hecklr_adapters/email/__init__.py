"""Email adapters: implementations of ``hecklr_schemas.EmailTarget``.

- ``smtp-imap`` (``SmtpImapEmailTarget``): SMTP out, IMAP polling in;
- ``webhook`` (``WebhookEmailTarget``): JSON POST out, replies POSTed back to a local endpoint;
- in-process (``PythonEmailTarget``): an async callable, for tests and in-process agents.

``make_email_target`` builds the first two from ``hecklr.yaml``. ``make_reply`` and friends build
correctly threaded messages, for agents and for the simulated customer alike.
"""

from hecklr_adapters.email.factory import make_email_target
from hecklr_adapters.email.messages import (
    from_mime,
    make_reply,
    new_message_id,
    reply_references,
    thread_subject,
    to_mime,
)
from hecklr_adapters.email.python import PythonEmailAgent, PythonEmailTarget
from hecklr_adapters.email.smtp_imap import SmtpImapEmailTarget
from hecklr_adapters.email.webhook import REPLY_PATH, WebhookEmailTarget

__all__ = [
    "REPLY_PATH",
    "PythonEmailAgent",
    "PythonEmailTarget",
    "SmtpImapEmailTarget",
    "WebhookEmailTarget",
    "from_mime",
    "make_email_target",
    "make_reply",
    "new_message_id",
    "reply_references",
    "thread_subject",
    "to_mime",
]
