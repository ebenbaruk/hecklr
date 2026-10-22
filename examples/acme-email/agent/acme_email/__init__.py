"""acme-email: the acme-maintenance reference agent reachable by email (WS12), and F11/F12.

The brains (LLM and rules) and their files are the chat agent's (``acme_agent``, imported); this
package is only the email front end: threading, quoted history, tool deadlines, and three
transports (webhook, SMTP/IMAP mailbox, in process).
"""

from acme_email.config import (
    EMAIL_ROOT,
    MAILER_FILE,
    EmailAgentFiles,
    MailerSettings,
    load_email_agent_files,
    load_mailer,
)
from acme_email.desk import SIGNATURE, EmailDesk, compose_reply
from acme_email.inprocess import InProcessEmailAgent
from acme_email.mailbox import Mailbox, serve_mailbox
from acme_email.tools import DeadlineTools, WorldRoute, route_world
from acme_email.webhook import create_app

__all__ = [
    "EMAIL_ROOT",
    "MAILER_FILE",
    "SIGNATURE",
    "DeadlineTools",
    "EmailAgentFiles",
    "EmailDesk",
    "InProcessEmailAgent",
    "Mailbox",
    "MailerSettings",
    "WorldRoute",
    "compose_reply",
    "create_app",
    "load_email_agent_files",
    "load_mailer",
    "route_world",
    "serve_mailbox",
]
