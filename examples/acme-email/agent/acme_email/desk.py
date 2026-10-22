"""``EmailDesk``: the acme agent answering email, whatever transport brought the email.

One conversation per customer address: the brain keeps its memory across the customer's emails,
as it does across chat messages. The brain reads only what is new in each email (quoted history
removed). Its answer goes back to the customer from the address they wrote to, in their thread
unless ``mailer.yaml`` says otherwise (F11).
"""

from typing import Final

from acme_agent.agent import AcmeAgent, BackendFactory

from acme_email.config import MailerSettings
from acme_email.tools import DeadlineTools
from hecklr_adapters.email import make_reply
from hecklr_channels.email import visible_text
from hecklr_schemas import EmailMessage

SIGNATURE: Final = "Acme Maintenance customer support"


def compose_reply(
    parent: EmailMessage, text: str, *, from_addr: str, threaded: bool
) -> EmailMessage:
    """The agent's answer to ``parent``; without ``threaded`` it loses the thread (F11)."""
    reply = make_reply(parent, body=f"{text}\n\n{SIGNATURE}", from_addr=from_addr)
    if threaded:
        return reply
    return reply.model_copy(update={"in_reply_to": None, "references": []})


class EmailDesk:
    """Answers customer emails with an ``AcmeAgent`` brain."""

    def __init__(self, agent: AcmeAgent, mailer: MailerSettings, variant: str) -> None:
        self.agent = agent
        self.mailer = mailer
        self.variant = variant

    async def answer(self, email: EmailMessage, backend: BackendFactory) -> EmailMessage:
        """The reply to one customer email; ``backend`` runs the conversation's tools.

        ``backend`` is called once, when the conversation starts (``AcmeAgent.respond``).

        Raises:
            hecklr_llm.LLMError: the LLM brain failed; the transport logs it and sends nothing.
        """
        timeout_s = self.mailer.tool_timeout_s
        reply = await self.agent.respond(
            email.from_addr.casefold(),
            visible_text(email.body) or email.subject,
            lambda: DeadlineTools(backend(), timeout_s),
        )
        return compose_reply(
            email,
            reply.text,
            from_addr=email.to_addrs[0],
            threaded=self.mailer.thread_replies,
        )
