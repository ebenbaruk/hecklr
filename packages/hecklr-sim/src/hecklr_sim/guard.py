"""Self-consistency guard: catches simulator turns that contradict the customer's brief.

A contradiction (the wrong customer ID, an invented order number, a changed goal) would make the
agent look wrong when the simulation is at fault, so flagged drafts are regenerated and counted.

The deterministic checks are cheap and run on every draft:

- **Wrong fact value:** a token shaped like a fact value (``C-1042`` → one letter, dash, four
  digits) that is not that value. Date and time values get no shape: a new date in the same
  format (the slot the customer asks for) is legitimate. A shape match inside a longer ID-like
  token is not a value of its own (the digits of ``INV-20482`` are not a postcode); that token
  is judged whole as an identifier instead.
- **Invented identifier:** an ID-like token (``ORD-5581``, ``4412345678``) found nowhere in the
  facts, the goal, the attack strategy's decoys or what the agent said.
- **Unknown fact:** ``facts_revealed`` names a fact the brief does not have: an entry is known
  when it is a fact's or a decoy's name, or a value the brief or the agent names as a whole token
  (the other customer the goal tells the customer to name, a decoy, an address the agent leaked).
- **Malformed decision:** ``speak`` with no text, ``end`` with no reason, or JSON spilled into
  the text (seen with small local models).

Tokens are matched whole (``ORD-558`` is not ``ORD-5581``). Tokens the brief names (its facts,
its goal, an attack strategy's decoys such as another customer's ID to probe) and tokens the agent
itself said are never flagged, so "no, not C-1043, it's C-1042" and the second invoice of a
duplicate charge named in the goal both pass. A changed goal cannot be detected by pattern
matching; the optional LLM check (``ConsistencyVerdict``) covers it.
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
from hecklr_sim.tokens import WORD_EDGE_AFTER, WORD_EDGE_BEFORE, mentions

IssueKind = Literal[
    "wrong_fact_value", "invented_identifier", "unknown_fact", "malformed", "llm_flagged"
]

_ID_TOKEN = re.compile(
    rf"{WORD_EDGE_BEFORE}(?:[A-Za-z]{{1,4}}-?\d{{3,}}|\d{{6,}}){WORD_EDGE_AFTER}"
)
"""ID-like tokens: letters then digits (``C-1042``, ``ORD5581``) or long digit runs (phones)."""
_RUNS = re.compile(r"[A-Za-z]+|\d+|[^A-Za-z\d]+")
_FORMAT_LEAK = re.compile(
    r"[{}]|\b(?:end_reason|facts_revealed|agent_made_progress|goal_achieved|goal_abandoned)\b"
)
"""Braces or answer-format field names in the text: the model spilled its JSON into the message."""
_NAME_VALUE = re.compile(r"[:=]")
"""Separates a ``facts_revealed`` entry written as ``name: value`` or ``name=value``."""
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_NOT_DASH_JOINED_BEFORE = r"(?<![^\W_]-)"
_NOT_DASH_JOINED_AFTER = r"(?!-[^\W_])"
"""A dash between letters or digits joins one identifier (``INV-20482``); it is not a token edge."""


@dataclass(frozen=True)
class GuardIssue:
    """One reason a drafted turn was rejected."""

    kind: IssueKind
    detail: str


class ConsistencyGuard:
    """Deterministic checks of drafted turns against one scenario's brief."""

    def __init__(self, scenario: Scenario, strategy: AttackStrategy | None = None) -> None:
        decoys = strategy.decoys if strategy is not None else {}
        self._values = {name: render_fact_value(value) for name, value in scenario.facts.items()}
        self._fact_names = {name.casefold() for name in [*scenario.facts, *decoys]}
        self._fact_values = {value.casefold() for value in _scalar_texts(scenario.facts)}
        self._brief_text = " ".join(
            [scenario.goal, *self._values.values(), *decoys.values()]
        ).casefold()
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
        issues += self._unknown_fact_issues(decision.facts_revealed, agent_text)
        return issues

    def _value_issues(self, text: str, agent_text: str) -> list[GuardIssue]:
        issues: list[GuardIssue] = []
        flagged: set[str] = set()
        id_spans = [match.span() for match in _ID_TOKEN.finditer(text)]
        for name, shape in self._shapes.items():
            for match in shape.finditer(text):
                token = match.group()
                folded = token.casefold()
                if (
                    _inside_longer_id(match.span(), id_spans)
                    or folded in flagged
                    or folded in self._fact_values
                    or _names(self._brief_text, folded)
                    or _names(agent_text, folded)
                ):
                    continue
                flagged.add(folded)
                issues.append(
                    GuardIssue(
                        "wrong_fact_value",
                        f"you wrote {token!r} but your {name} is {self._values[name]!r}",
                    )
                )
        # ID tokens are whole tokens already, so plain ``mentions`` exempts them correctly.
        for token in _ID_TOKEN.findall(text):
            folded = token.casefold()
            if (
                folded in flagged
                or mentions(self._brief_text, folded)
                or mentions(agent_text, folded)
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

    def _unknown_fact_issues(self, revealed: Iterable[str], agent_text: str) -> list[GuardIssue]:
        return [
            GuardIssue("unknown_fact", f"{entry!r} is not one of your facts; do not invent facts")
            for entry in revealed
            if not self._is_known_fact(entry, agent_text)
        ]

    def _is_known_fact(self, entry: str, agent_text: str) -> bool:
        # Models report a fact by name, by value, or as "name: value" (with a name of their own
        # for someone else's detail, e.g. "brother's name: Martin Dubois"). What the brief or the
        # agent names is not invented, matching what the text checks accept (issue #68).
        name, *value = (part.strip().casefold() for part in _NAME_VALUE.split(entry, maxsplit=1))
        if name in self._fact_names:
            return True
        return any(
            self._is_named_value(said, agent_text) for said in (entry.strip().casefold(), *value)
        )

    def _is_named_value(self, said: str, agent_text: str) -> bool:
        if said in self._fact_values:
            return True
        # One character names nothing; it would match every stray letter of the brief.
        return len(said) >= 2 and (mentions(self._brief_text, said) or mentions(agent_text, said))


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
    return re.compile(rf"{WORD_EDGE_BEFORE}{''.join(parts)}{WORD_EDGE_AFTER}")


def _inside_longer_id(span: tuple[int, int], id_spans: Iterable[tuple[int, int]]) -> bool:
    """Whether ``span`` lies within, and is shorter than, one of the ID-like tokens' spans."""
    start, end = span
    return any(s <= start and end <= e and (s, e) != span for s, e in id_spans)


def _names(text: str, token: str) -> bool:
    """Whether ``text`` names ``token`` on its own; ``20482`` inside ``INV-20482`` is not it."""
    pattern = (
        rf"{WORD_EDGE_BEFORE}{_NOT_DASH_JOINED_BEFORE}{re.escape(token)}"
        rf"{_NOT_DASH_JOINED_AFTER}{WORD_EDGE_AFTER}"
    )
    return re.search(pattern, text) is not None


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
