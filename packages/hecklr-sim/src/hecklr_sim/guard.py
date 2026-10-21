"""Self-consistency guard: catches simulator turns that contradict the customer's brief.

A contradiction (the wrong customer ID, an invented order number, a changed goal) would make the
agent look wrong when the simulation is at fault, so flagged drafts are regenerated and counted.

The deterministic checks are cheap and run on every draft:

- **Wrong fact value:** a token shaped like a fact value (``C-1042`` → one letter, dash, four
  digits) that is not that value. Date and time values get no shape: a new date in the same
  format (the slot the customer asks for) is legitimate.
- **Invented identifier:** an ID-like token (``ORD-5581``, ``4412345678``) found nowhere in the
  facts, the goal, the attack strategy's decoys or what the agent said.
- **Unknown fact:** ``facts_revealed`` names a fact the brief does not have.
- **Malformed decision:** ``speak`` with no text, ``end`` with no reason, or JSON spilled into
  the text (seen with small local models).

Tokens are matched whole (``ORD-558`` is not ``ORD-5581``). Tokens the agent itself said and an
attack strategy's decoys (another customer's ID to probe) are never flagged, so "no, not C-1043,
it's C-1042" passes. A changed
goal cannot be detected by pattern matching; the optional LLM check (``ConsistencyVerdict``)
covers it.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from hecklr_schemas import DialogueTurn, Scenario
from hecklr_sim.brief import render_fact_value
from hecklr_sim.decision import SimDecision
from hecklr_sim.strategies import AttackStrategy

IssueKind = Literal[
    "wrong_fact_value", "invented_identifier", "unknown_fact", "malformed", "llm_flagged"
]

_ID_TOKEN = re.compile(r"(?<![A-Za-z\d])(?:[A-Za-z]{1,4}-?\d{3,}|\d{6,})(?![A-Za-z\d])")
"""ID-like tokens: letters then digits (``C-1042``, ``ORD5581``) or long digit runs (phones)."""
_RUNS = re.compile(r"[A-Za-z]+|\d+|[^A-Za-z\d]+")
_FORMAT_LEAK = re.compile(
    r"[{}]|\b(?:end_reason|facts_revealed|agent_made_progress|goal_achieved|goal_abandoned)\b"
)
"""Braces or answer-format field names in the text: the model spilled its JSON into the message."""
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class GuardIssue:
    """One reason a drafted turn was rejected."""

    kind: IssueKind
    detail: str


class ConsistencyGuard:
    """Deterministic checks of drafted turns against one scenario's brief."""

    def __init__(self, scenario: Scenario, strategy: AttackStrategy | None = None) -> None:
        decoys = strategy.decoys.values() if strategy is not None else ()
        self._values = {name: render_fact_value(value) for name, value in scenario.facts.items()}
        self._fact_names = {name.casefold() for name in scenario.facts}
        self._fact_values = {value.casefold() for value in _scalar_texts(scenario.facts)}
        self._allowed_values = self._fact_values | {decoy.casefold() for decoy in decoys}
        self._brief_text = " ".join([scenario.goal, *self._values.values(), *decoys]).casefold()
        # Only scalar facts get a shape; a nested fact rendered as JSON is not an identifier.
        self._shapes = {
            name: shape
            for name, value in scenario.facts.items()
            if isinstance(value, str | int)
            and not isinstance(value, bool)
            and (shape := _shape(str(value)))
        }

    def check(self, decision: SimDecision, dialogue: Sequence[DialogueTurn]) -> list[GuardIssue]:
        """Return every issue with ``decision`` given the visible ``dialogue``; empty if none."""
        issues = _structural_issues(decision)
        agent_text = " ".join(turn.text for turn in dialogue if turn.speaker == "agent").casefold()
        issues += self._value_issues(decision.text, agent_text)
        issues += self._unknown_fact_issues(decision.facts_revealed)
        return issues

    def _value_issues(self, text: str, agent_text: str) -> list[GuardIssue]:
        issues: list[GuardIssue] = []
        flagged: set[str] = set()
        for name, shape in self._shapes.items():
            for token in shape.findall(text):
                folded = token.casefold()
                if (
                    folded in flagged
                    or folded in self._allowed_values
                    or _mentions(agent_text, folded)
                ):
                    continue
                flagged.add(folded)
                issues.append(
                    GuardIssue(
                        "wrong_fact_value",
                        f"you wrote {token!r} but your {name} is {self._values[name]!r}",
                    )
                )
        for token in _ID_TOKEN.findall(text):
            folded = token.casefold()
            if (
                folded in flagged
                or _mentions(self._brief_text, folded)
                or _mentions(agent_text, folded)
            ):
                continue
            flagged.add(folded)
            issues.append(
                GuardIssue(
                    "invented_identifier",
                    f"{token!r} is not in your brief; never invent identifiers or numbers",
                )
            )
        return issues

    def _unknown_fact_issues(self, revealed: Iterable[str]) -> list[GuardIssue]:
        return [
            GuardIssue("unknown_fact", f"{entry!r} is not one of your facts; do not invent facts")
            for entry in revealed
            if not self._is_known_fact(entry)
        ]

    def _is_known_fact(self, entry: str) -> bool:
        # Models report a fact by name, by value, or as "name: value"; all name the same fact.
        name = re.split(r"[:=]", entry, maxsplit=1)[0].strip().casefold()
        return name in self._fact_names or entry.strip().casefold() in self._fact_values


def _structural_issues(decision: SimDecision) -> list[GuardIssue]:
    if decision.action == "speak" and not decision.text.strip():
        return [GuardIssue("malformed", 'action "speak" needs a non-empty text')]
    if decision.action == "end" and decision.end_reason is None:
        return [GuardIssue("malformed", 'action "end" needs an end_reason')]
    if _FORMAT_LEAK.search(decision.text):
        return [GuardIssue("malformed", "text must be only your message: no JSON, no field names")]
    return []


def _shape(value: str) -> re.Pattern[str] | None:
    """A regex matching strings with ``value``'s shape, for values that look like identifiers.

    Only values with a digit and at least 3 characters get a shape: free-text facts such as a
    name would match far too much. Dates and times get none: asking for a new date in the same
    format is normal, not a contradiction.
    """
    if len(value) < 3 or not any(char.isdigit() for char in value) or _ISO_DATE.match(value):
        return None
    parts = []
    for run in _RUNS.findall(value):
        if run[0].isdigit():
            parts.append(rf"\d{{{len(run)}}}")
        elif run[0].isalpha():
            parts.append(rf"[A-Za-z]{{{len(run)}}}")
        else:
            parts.append(re.escape(run))
    return re.compile(rf"(?<![A-Za-z\d]){''.join(parts)}(?![A-Za-z\d])")


def _mentions(text: str, token: str) -> bool:
    """Whether ``token`` occurs in ``text`` as a whole token, not as part of a longer one."""
    return re.search(rf"(?<![A-Za-z\d]){re.escape(token)}(?![A-Za-z\d])", text) is not None


def _scalar_texts(value: JsonValue) -> Iterable[str]:
    """Text of every scalar inside a fact value, so nested facts count as known values."""
    if isinstance(value, dict):
        for item in value.values():
            yield from _scalar_texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _scalar_texts(item)
    elif value is not None:
        yield render_fact_value(value)
