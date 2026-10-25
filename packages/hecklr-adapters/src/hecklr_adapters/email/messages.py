"""Email messages on the wire: RFC 5322 MIME to and from ``EmailMessage``, and reply threading.

Both sides of an email conversation use the same threading rule (RFC 5322 section 3.6.4): a reply
names its parent in ``In-Reply-To`` and carries the parent's ``References`` (or, lacking those,
its ``In-Reply-To``) followed by the parent's ``Message-ID``.
"""

import re
from email import message_from_bytes, policy
from email.message import EmailMessage as MimeMessage
from email.message import Message
from email.utils import getaddresses, parseaddr

from pydantic import ValidationError

from hecklr_adapters.errors import AgentError
from hecklr_schemas import EmailAttachment, EmailMessage, new_id

_REPLY_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|sv)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)
_MSG_ID = re.compile(r"<[^<>\s]+>")


def new_message_id(domain: str) -> str:
    """A fresh, globally unique ``Message-ID`` (a ULID) in angle brackets."""
    return f"<{new_id()}@{domain}>"


def thread_subject(subject: str) -> str:
    """The subject without ``Re:``/``Fwd:`` prefixes, as a reply's ``Re:`` is added to it."""
    return _REPLY_PREFIX.sub("", subject).strip()


def reply_references(parent: EmailMessage) -> list[str]:
    """The ``References`` of a reply to ``parent``: the parent's chain, then the parent."""
    if parent.references:
        chain = list(parent.references)
    elif parent.in_reply_to is not None:
        chain = [parent.in_reply_to]
    else:
        chain = []
    return [*chain, parent.message_id]


def make_reply(
    parent: EmailMessage,
    *,
    body: str,
    from_addr: str,
    attachments: list[EmailAttachment] | None = None,
) -> EmailMessage:
    """A correctly threaded reply to ``parent``, sent back to its sender.

    The ``Message-ID`` domain is the sender's, as a mail client would generate it.
    """
    return EmailMessage(
        message_id=new_message_id(from_addr.rpartition("@")[2]),
        in_reply_to=parent.message_id,
        references=reply_references(parent),
        subject=f"Re: {thread_subject(parent.subject)}",
        body=body,
        from_addr=from_addr,
        to_addrs=[parent.from_addr],
        attachments=attachments or [],
    )


def to_mime(msg: EmailMessage, headers: dict[str, str] | None = None) -> MimeMessage:
    """Render ``msg`` as a MIME message ready for SMTP, with any extra ``headers``."""
    mime = MimeMessage(policy=policy.SMTP)
    mime["Message-ID"] = msg.message_id
    if msg.in_reply_to is not None:
        mime["In-Reply-To"] = msg.in_reply_to
    if msg.references:
        mime["References"] = " ".join(msg.references)
    mime["Subject"] = msg.subject
    mime["From"] = msg.from_addr
    mime["To"] = ", ".join(msg.to_addrs)
    for name, value in (headers or {}).items():
        mime[name] = value
    mime.set_content(msg.body)
    for attachment in msg.attachments:
        maintype, _, subtype = attachment.content_type.partition("/")
        mime.add_attachment(
            attachment.data,
            maintype=maintype,
            subtype=subtype or "octet-stream",
            filename=attachment.filename,
        )
    return mime


def from_mime(raw: bytes) -> EmailMessage:
    """Parse a received RFC 5322 message.

    A message without a ``Message-ID`` gets a synthetic one under ``hecklr.invalid`` so it can
    still be logged and graded (it then cannot be a thread parent anyone names).

    Raises:
        AgentError: the message lacks a usable sender or recipient, or cannot be decoded to
            storable text (an unknown charset, or one that yields a lone surrogate).
    """
    try:
        return storable(_parse(raw))
    except (UnicodeError, LookupError) as exc:
        # The agent's bytes are at fault, so this is its error, not the harness's (issue #119).
        raise AgentError(f"agent sent an email that cannot be decoded: {exc}") from exc


def storable(msg: EmailMessage) -> EmailMessage:
    """``msg``, once checked to have a UTF-8 form, so the harness can store it.

    A lone UTF-16 surrogate (``json.loads`` of an emoji escape cut in half, or a charset such as
    ``unicode-escape``) has none: storing it would fail inside the harness, and the agent's fault
    would be retried and excluded as a harness error (issues #112, #119).

    Raises:
        AgentError: some text of ``msg`` is not valid Unicode.
    """
    try:
        msg.model_dump_json()
    except ValueError as exc:  # pydantic's serialization error wraps the UnicodeEncodeError
        raise AgentError(
            f"agent sent an email whose text is not valid Unicode (an unpaired surrogate): {exc}"
        ) from exc
    return msg


def _parse(raw: bytes) -> EmailMessage:
    mime = message_from_bytes(raw, _class=MimeMessage, policy=policy.default)
    msg_ids = _MSG_ID.findall(str(mime.get("Message-ID", "")))
    in_reply_to = _MSG_ID.findall(str(mime.get("In-Reply-To", "")))
    recipients = getaddresses([str(v) for v in mime.get_all("To", []) + mime.get_all("Cc", [])])
    try:
        return EmailMessage(
            message_id=msg_ids[0] if msg_ids else new_message_id("hecklr.invalid"),
            in_reply_to=in_reply_to[0] if in_reply_to else None,
            references=_MSG_ID.findall(str(mime.get("References", ""))),
            subject=str(mime.get("Subject", "")),
            body=_body_text(mime),
            from_addr=parseaddr(str(mime.get("From", "")))[1],
            to_addrs=[addr for _, addr in recipients if addr],
            attachments=[_attachment(part) for part in mime.iter_attachments()],
        )
    except ValidationError as exc:
        raise AgentError(f"agent sent a malformed email: {exc.errors()[0]['msg']}") from exc


def _body_text(mime: MimeMessage) -> str:
    part = mime.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    text = content if isinstance(content, str) else content.decode(errors="replace")
    # Wire line endings are CRLF; MIME text parts also end with a line break the sender never
    # typed (``to_mime`` adds one too).
    return text.replace("\r\n", "\n").removesuffix("\n")


def _attachment(part: Message) -> EmailAttachment:
    payload = part.get_payload(decode=True)
    return EmailAttachment(
        filename=part.get_filename() or "attachment",
        content_type=part.get_content_type(),
        data=payload if isinstance(payload, bytes) else b"",
    )
