"""How a customer quotes the email they answer, and how quoted history is read back out.

Real customers reply in different styles, and an email agent must cope with all of them: the new
text above the quoted message (``top_post``, the default of most mail clients), below it
(``bottom_post``), or with no quote at all (``none``). Each persona keeps one style for every
trial, chosen from its id, as a person keeps their mail client.
"""

import random
import re
from typing import Final, Literal, get_args

from hecklr_schemas import EmailMessage

QuoteStyle = Literal["top_post", "bottom_post", "none"]
QUOTE_STYLES: Final[tuple[QuoteStyle, ...]] = get_args(QuoteStyle)

_ATTRIBUTION = re.compile(r"^\s*On .+ wrote:\s*$")
_FORWARD_MARKER = re.compile(r"^\s*-{2,}\s*(Original Message|Forwarded message)\s*-{2,}\s*$", re.I)


def persona_quote_style(persona_id: str) -> QuoteStyle:
    """The quoting style of ``persona_id``, the same in every trial."""
    rng = random.Random(f"hecklr-quote-style:{persona_id}")  # noqa: S311 -- simulation, not security
    return rng.choice(QUOTE_STYLES)


def quote(text: str, parent: EmailMessage, style: QuoteStyle) -> str:
    """The body of a reply saying ``text`` to ``parent``, quoting it in ``style``."""
    if style == "none":
        return text
    quoted = "\n".join(f"> {line}" if line else ">" for line in _lines(parent.body))
    block = f"On a previous message, {parent.from_addr} wrote:\n{quoted}"
    return f"{text}\n\n{block}" if style == "top_post" else f"{block}\n\n{text}"


def visible_text(body: str) -> str:
    """What is new in ``body``: quoted lines, attributions and forwarded history removed.

    The simulated customer reads replies the way a person does, ignoring the history the agent's
    mail client quoted back.
    """
    kept: list[str] = []
    for line in _lines(body):
        if _FORWARD_MARKER.match(line):
            break
        if line.lstrip().startswith(">") or _ATTRIBUTION.match(line):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _lines(body: str) -> list[str]:
    # Mail lines end in CRLF or LF only; str.splitlines would also split on form feeds and other
    # Unicode separators that are ordinary characters in a message body.
    return body.replace("\r\n", "\n").split("\n")
