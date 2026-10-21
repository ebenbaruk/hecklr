"""Self-consistency guard: catches simulator turns that contradict the customer's brief.

A contradiction (the wrong customer ID, an invented order number, a changed goal) would make the
agent look wrong when the simulation is at fault, so flagged drafts are regenerated and counted.

The deterministic checks are cheap and run on every draft:

- **Wrong fact value:** a token shaped like a fact value (``C-1042`` → one letter, dash, four
  digits) that is not that value.
- **Invented identifier:** an ID-like token (``ORD-5581``, ``4412345678``) found nowhere in the
  facts, the goal or what the agent said.
- **Unknown fact:** ``facts_revealed`` names a fact the brief does not have.
- **Malformed decision:** ``speak`` with no text, or ``end`` with no reason.

Tokens the agent itself said are never flagged, so "no, not C-1043, it's C-1042" passes. A changed
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

IssueKind = Literal[
    "wrong_fact_value", "invented_identifier", "unknown_fact", "malformed", "llm_flagged"
]

_ID_TOKEN = re.compile(r"(?<![A-Za-z\d])(?:[A-Za-z]{1,4}-?\d{3,}|\d{6,})(?![A-Za-z\d])")
"""ID-like tokens: letters then digits (``C-1042``, ``ORD5581``) or long digit runs (phones)."""
_RUNS = re.compile(r"[A-Za-z]+|\d+|[^A-Za-z\d]+")


@dataclass(frozen=True)
class GuardIssue:
    """One reason a drafted turn was rejected."""

    kind: IssueKind
    detail: str


class ConsistencyGuard:
    """Deterministic checks of drafted turns against one scenario's brief."""

    def __init__(self, scenario: Scenario) -> None:
        self._values = {name: render_fact_value(value) for name, value in scenario.facts.items()}
        self._fact_names = {name.casefold() for name in scenario.facts}
        self._fact_values = {value.casefold() for value in _scalar_texts(scenario.facts)}
        self._brief_text = " ".join([scenario.goal, *self._values.values()]).casefold()
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
                if folded in self._fact_values or folded in agent_text or folded in flagged:
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
            if folded in flagged or folded in self._brief_text or folded in agent_text:
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
            GuardIssue("unknown_fact", f"{name!r} is not one of your facts; do not invent facts")
            for name in revealed
            if name.casefold() not in self._fact_names and name.casefold() not in self._fact_values
        ]


def _structural_issues(decision: SimDecision) -> list[GuardIssue]:
    if decision.action == "speak" and not decision.text.strip():
        return [GuardIssue("malformed", 'action "speak" needs a non-empty text')]
    if decision.action == "end" and decision.end_reason is None:
        return [GuardIssue("malformed", 'action "end" needs an end_reason')]
    return []


def _shape(value: str) -> re.Pattern[str] | None:
    """A regex matching strings with ``value``'s shape, for values that look like identifiers.

    Only values with a digit and at least 3 characters get a shape: free-text facts such as a
    name would match far too much.
    """
    if len(value) < 3 or not any(char.isdigit() for char in value):
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
