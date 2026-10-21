"""What the rules brain reads from the editable files, so that a fault is a text change.

The LLM brain follows the system prompt and tool descriptions because a model reads them. The
rules brain reads the same files with these patterns: each rule of the prompt that a fault removes
or rewrites switches one behaviour, and the slot format a tool description declares is the format
the brain writes. A prompt that keeps a rule's meaning in other words should keep its keywords
(e.g. "before booking ... confirm ... slot") for the rules brain to see it.
"""

import re
from dataclasses import dataclass
from typing import Final

from acme_agent.config import AgentFiles

CONFIRM_SLOTS: Final = re.compile(r"before (booking|rescheduling)[^\n]*confirm[^\n]*slot", re.I)
VERIFY_REFUNDS: Final = re.compile(
    r"refund[^\n]*\bverify\b[^\n]*before|\bverify\b[^\n]*before[^\n]*refund", re.I
)
PROTECT_OTHERS: Final = re.compile(
    r"never (reveal|share|disclose|confirm)[^\n]*(anyone|another|other customer)", re.I
)
HAND_OFF_EMERGENCIES: Final = re.compile(
    r"emergenc[^\n]*(?<!never )call create_emergency_ticket", re.I
)
CANCELLATION_FEE: Final = re.compile(
    r"(€\s?\d+(?:[.,]\d{2})?)[^\n.]*cancellation fee"
    r"|cancellation fee[^\n.]*?(€\s?\d+(?:[.,]\d{2})?)",
    re.I,
)
SLOT_FORMAT: Final = re.compile(r"(YYYY|MM|DD)([-/.])(YYYY|MM|DD)\2(YYYY|MM|DD)(?:([T ])HH:MM)?")
_FIELDS: Final = {"YYYY": "%Y", "MM": "%m", "DD": "%d"}
ISO_SLOT: Final = "%Y-%m-%dT%H:%M"


@dataclass(frozen=True, slots=True)
class Behaviour:
    """The rules brain's switches, derived from the agent files."""

    confirm_slots: bool
    """Offer slots matching the customer's preferences and wait for a yes before booking."""
    verify_refunds: bool
    hand_off_emergencies: bool
    protect_others: bool
    cancellation_fee: str | None
    """A cancellation fee the prompt asserts (e.g. ``€45``), which no policy contains."""
    slot_formats: dict[str, str]
    """``strftime`` format of the ``slot`` argument, per tool that takes one."""

    @classmethod
    def from_files(cls, files: AgentFiles) -> "Behaviour":
        """Read the switches from the system prompt and the tool catalog."""
        prompt = files.system_prompt
        fee = CANCELLATION_FEE.search(prompt)
        return cls(
            confirm_slots=bool(CONFIRM_SLOTS.search(prompt)),
            verify_refunds=bool(VERIFY_REFUNDS.search(prompt)),
            hand_off_emergencies=bool(HAND_OFF_EMERGENCIES.search(prompt)),
            protect_others=bool(PROTECT_OTHERS.search(prompt)),
            cancellation_fee=(fee.group(1) or fee.group(2)).replace(" ", "") if fee else None,
            slot_formats={
                name: slot_format(spec.parameter_description("slot"))
                for name, spec in files.tools.items()
                if spec.parameter_description("slot")
            },
        )

    def slot_format(self, tool: str) -> str:
        """The ``strftime`` format ``tool`` declares for its slot (ISO 8601 by default)."""
        return self.slot_formats.get(tool, ISO_SLOT)


def slot_format(description: str) -> str:
    """Translate a declared date format such as ``YYYY-MM-DDTHH:MM`` into ``strftime``."""
    match = SLOT_FORMAT.search(description)
    if match is None:
        return ISO_SLOT
    first, separator, second, third, time_separator = match.groups()
    date_part = separator.join(_FIELDS[field] for field in (first, second, third))
    return f"{date_part}{time_separator}%H:%M" if time_separator else date_part
