"""MIME round trips and reply threading (RFC 5322 section 3.6.4)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hecklr_adapters import AgentError
from hecklr_adapters.email import (
    from_mime,
    make_reply,
    new_message_id,
    reply_references,
    thread_subject,
    to_mime,
)
from hecklr_schemas import TRIAL_ID_HEADER, EmailAttachment, EmailMessage

CUSTOMER = "camille.01abc@customers.hecklr.test"
AGENT = "support@acme.test"


def first_email(**changes: object) -> EmailMessage:
    message = EmailMessage(
        message_id="<c1@customers.hecklr.test>",
        subject="Boiler visit",
        body="Can we move Thursday's visit?\nThanks",
        from_addr=CUSTOMER,
        to_addrs=[AGENT],
    )
    return message.model_copy(update=changes)


def test_ws12_mime_round_trip_keeps_threading_headers_and_attachments() -> None:
    pdf = EmailAttachment(filename="invoice.pdf", content_type="application/pdf", data=b"%PDF-1")
    reply = make_reply(first_email(), body="Moved to Tuesday.", from_addr=AGENT, attachments=[pdf])
    mime = to_mime(reply, {TRIAL_ID_HEADER: "01TRIAL"})

    assert mime[TRIAL_ID_HEADER] == "01TRIAL"
    assert from_mime(mime.as_bytes()) == reply


def test_ws12_make_reply_threads_to_the_parent() -> None:
    reply = make_reply(first_email(), body="ok", from_addr=AGENT)
    assert reply.in_reply_to == "<c1@customers.hecklr.test>"
    assert reply.references == ["<c1@customers.hecklr.test>"]
    assert reply.subject == "Re: Boiler visit"
    assert reply.to_addrs == [CUSTOMER]
    assert reply.message_id.endswith("@acme.test>")


def test_ws12_reply_references_extend_the_chain_or_fall_back_to_in_reply_to() -> None:
    chained = first_email(references=["<a@x>", "<b@x>"], in_reply_to="<b@x>")
    assert reply_references(chained) == ["<a@x>", "<b@x>", "<c1@customers.hecklr.test>"]
    only_parent = first_email(in_reply_to="<b@x>")
    assert reply_references(only_parent) == ["<b@x>", "<c1@customers.hecklr.test>"]


@given(st.lists(st.sampled_from(["Re: ", "RE:", "Fwd: ", "re[2]: ", "AW: "]), max_size=4))
def test_ws12_thread_subject_strips_any_reply_prefixes(prefixes: list[str]) -> None:
    assert thread_subject("".join(prefixes) + "Boiler visit") == "Boiler visit"


def test_ws12_message_ids_are_unique_and_bracketed() -> None:
    ids = {new_message_id("acme.test") for _ in range(100)}
    assert len(ids) == 100
    assert all(i.startswith("<") and i.endswith("@acme.test>") for i in ids)


def _raw(headers: str, body: str = "hello") -> bytes:
    return f"{headers}\r\n\r\n{body}".encode()


def test_ws12_from_mime_reads_names_cc_and_multiple_references() -> None:
    raw = _raw(
        "From: Acme Support <support@acme.test>\r\n"
        "To: Camille <c@customers.hecklr.test>\r\nCc: boss@acme.test\r\n"
        "Subject: Re: x\r\nMessage-ID: <m@acme.test>\r\n"
        "In-Reply-To: <p@c.test>\r\nReferences: <a@c.test>\r\n <p@c.test>"
    )
    message = from_mime(raw)
    assert message.from_addr == "support@acme.test"
    assert message.to_addrs == ["c@customers.hecklr.test", "boss@acme.test"]
    assert (message.in_reply_to, message.references) == ("<p@c.test>", ["<a@c.test>", "<p@c.test>"])
    assert message.body == "hello"


def test_ws12_a_message_without_message_id_gets_a_synthetic_one() -> None:
    message = from_mime(_raw("From: a@b.test\r\nTo: c@d.test\r\nSubject: s"))
    assert message.message_id.endswith("@hecklr.invalid>")
    assert message.in_reply_to is None


def test_ws12_an_html_only_body_is_read() -> None:
    raw = _raw(
        "From: a@b.test\r\nTo: c@d.test\r\nSubject: s\r\nContent-Type: text/html", "<p>Hi</p>"
    )
    assert from_mime(raw).body.strip() == "<p>Hi</p>"


def test_ws12_a_body_less_multipart_reads_as_empty() -> None:
    raw = _raw(
        "From: a@b.test\r\nTo: c@d.test\r\nSubject: s\r\nMIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="B"',
        "--B\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment\r\n\r\nx\r\n--B--",
    )
    message = from_mime(raw)
    assert message.body == ""
    assert [a.filename for a in message.attachments] == ["attachment"]


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param("To: c@d.test\r\nSubject: s", id="no-sender"),
        pytest.param("From: a@b.test\r\nSubject: s", id="no-recipient"),
    ],
)
def test_ws12_a_malformed_agent_email_is_an_agent_error(headers: str) -> None:
    with pytest.raises(AgentError, match="malformed email"):
        from_mime(_raw(headers))


_ADDRESSES = "From: a@b.test\r\nTo: c@d.test\r\n"


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        # An emoji escape cut in half, in a body whose declared charset decodes escapes.
        pytest.param(
            "Subject: s\r\nContent-Type: text/plain; charset=unicode-escape",
            "Sure \\ud83d",
            id="body-surrogate",
        ),
        pytest.param("Subject: =?unicode-escape?q?Hi_\\ud83d?=", "x", id="subject-surrogate"),
        pytest.param(
            "Subject: s\r\nContent-Type: text/plain; charset=no-such-codec", "x", id="unknown-codec"
        ),
    ],
)
def test_issue119_an_undecodable_agent_email_is_an_agent_error(headers: str, body: str) -> None:
    # Before #119 these escaped as UnicodeEncodeError, LookupError or, worse, a message that
    # failed only when stored: a harness error, retried and excluded, for the agent's fault.
    with pytest.raises(AgentError, match=r"cannot be decoded|not valid Unicode"):
        from_mime(_raw(_ADDRESSES + headers, body))


def test_issue119_replaced_bytes_still_read() -> None:
    message = from_mime(_ADDRESSES.encode() + b"Subject: Hi \xff\r\n\r\nBody")
    assert message.subject == "Hi �"
    message.model_dump_json()  # storable
