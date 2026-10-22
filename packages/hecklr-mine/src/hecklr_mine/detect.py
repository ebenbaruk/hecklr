"""Stage 3, Detect failures: which production conversations went wrong, and how.

Four cheap heuristics run on every conversation (escalation to a human, abandonment, repeated
questions, a negative sentiment trend); ``hecklr_grade`` judges optionally add an LLM verdict per
rubric. A conversation with any signal is a failure; its ``summary`` is what stage 4 embeds.
"""

import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Final

import anyio
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from hecklr_mine.judges import judge_conversation
from hecklr_schemas import Conversation, ConversationTurn, LLMProvider


class SignalKind(StrEnum):
    """Kinds of failure evidence."""

    ESCALATION = "escalation"
    """The customer asked for a human, or the agent handed off."""
    ABANDONMENT = "abandonment"
    """The customer gave up, or left a message the agent never answered."""
    REPEATED_QUESTION = "repeated_question"
    """The customer had to repeat themselves, or the agent asked the same question again."""
    NEGATIVE_SENTIMENT = "negative_sentiment"
    """The customer's mood got clearly worse over the conversation."""
    JUDGE = "judge"
    """An LLM judge failed the conversation on a rubric."""


class Signal(BaseModel):
    """One piece of evidence that a conversation failed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SignalKind
    turn: int | None = None
    """Index of the turn that shows it, when one does."""
    detail: str


class FailureReport(BaseModel):
    """What detection found in one conversation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: str
    signals: list[Signal] = Field(default_factory=list)
    judge_errors: list[str] = Field(default_factory=list)
    """Judges that reached no verdict: not evidence of failure, but never silently dropped."""
    summary: str = ""
    """Text that stands for the failure in clustering (empty when nothing failed)."""

    @property
    def failed(self) -> bool:
        """Whether any signal fired."""
        return bool(self.signals)


# --- escalation ----------------------------------------------------------------------------

_ASK_FOR_HUMAN = re.compile(
    r"\b(?:speak|talk|chat)\s+(?:to|with)\s+(?:a|an|the|some|your)?\s*(?:real\s+|actual\s+)?"
    r"(?:human|person|agent|manager|supervisor|someone|representative)\b"
    r"|\b(?:real|live|actual)\s+(?:person|human|agent)\b"
    r"|\b(?:human|manager|supervisor)\s*(?:,\s*)?(?:please|now)\b",
    re.IGNORECASE,
)
_HANDOFF = re.compile(
    r"\b(?:transfer|connect|escalat|pass|hand)\w*\b[^.?!]{0,40}?"
    r"\b(?:human|colleague|agent|specialist|team|supervisor|manager)\b",
    re.IGNORECASE,
)
_ESCALATION_FLAGS: Final = ("escalated", "escalation", "handoff", "handed_off", "transferred")
_OUTCOME_KEYS: Final = ("outcome", "resolution", "status", "end_reason")


def _flagged(metadata: Mapping[str, JsonValue], values: Sequence[str]) -> str | None:
    for key in values:
        if metadata.get(key) is True:
            return key
    for key in _OUTCOME_KEYS:
        value = metadata.get(key)
        if isinstance(value, str) and value.casefold() in values:
            return f"{key}={value}"
    return None


def escalation(conversation: Conversation) -> list[Signal]:
    """The customer asked for a human, the agent handed off, or the log says it escalated."""
    flag = _flagged(conversation.metadata, _ESCALATION_FLAGS)
    if flag is not None:
        return [Signal(kind=SignalKind.ESCALATION, detail=f"logged as escalated ({flag})")]
    for index, turn in enumerate(conversation.turns):
        pattern = _ASK_FOR_HUMAN if turn.role == "user" else _HANDOFF
        match = pattern.search(turn.text)
        if match:
            who = "customer asked for" if turn.role == "user" else "agent handed off to"
            detail = f"{who} a human: {match.group()!r}"
            return [Signal(kind=SignalKind.ESCALATION, turn=index, detail=detail)]
    return []


# --- abandonment ---------------------------------------------------------------------------

_GIVE_UP = re.compile(
    r"\b(?:forget\s+it|never\s*mind|i\s+give\s+up|this\s+is\s+(?:useless|pointless|ridiculous)"
    r"|waste\s+of\s+(?:my\s+)?time|(?:go|going)\s+(?:elsewhere|to\s+a\s+competitor)"
    r"|i'?ll\s+call\s+(?:back|someone\s+else)|cancel\s+(?:my\s+)?(?:account|contract))\b",
    re.IGNORECASE,
)
_CLOSING = re.compile(
    r"^\W*(?:ok(?:ay)?|thanks?|thank\s+you|cheers|bye|goodbye|great|perfect|got\s+it)\b",
    re.IGNORECASE,
)
_ABANDON_FLAGS: Final = ("abandoned", "dropped", "timeout", "no_reply")


def abandonment(conversation: Conversation) -> list[Signal]:
    """The customer gave up near the end, or their last message went unanswered."""
    flag = _flagged(conversation.metadata, _ABANDON_FLAGS)
    if flag is not None:
        return [Signal(kind=SignalKind.ABANDONMENT, detail=f"logged as abandoned ({flag})")]
    users = [(i, t) for i, t in enumerate(conversation.turns) if t.role == "user"]
    for index, turn in users[-2:]:
        match = _GIVE_UP.search(turn.text)
        if match:
            detail = f"customer gave up: {match.group()!r}"
            return [Signal(kind=SignalKind.ABANDONMENT, turn=index, detail=detail)]
    last_index, last = len(conversation.turns) - 1, conversation.turns[-1]
    if last.role == "user" and len(users) > 1 and not _CLOSING.match(last.text):
        detail = "the customer's last message was never answered"
        return [Signal(kind=SignalKind.ABANDONMENT, turn=last_index, detail=detail)]
    return []


# --- repeated questions --------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
# fmt: off
STOPWORDS: Final = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "to", "of", "in", "on", "at", "for", "with",
    "from", "by", "is", "are", "was", "were", "be", "been", "am", "i", "you", "he", "she",
    "it", "we", "they", "me", "my", "your", "our", "their", "this", "that", "these",
    "those", "do", "does", "did", "have", "has", "had", "can", "could", "will", "would",
    "should", "please", "just", "so", "as", "not", "no", "yes", "ok", "okay", "hi", "hello",
    "there", "here", "what", "which", "who", "how", "when", "where", "why",
})
# fmt: on
_MIN_CONTENT_WORDS: Final = 3


def content_words(text: str) -> set[str]:
    """Lower-case words of ``text`` without stop words."""
    return {word for word in _WORD.findall(text.casefold()) if word not in STOPWORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def repeated_questions(
    conversation: Conversation, *, customer_overlap: float = 0.6, agent_overlap: float = 0.8
) -> list[Signal]:
    """A customer message that largely repeats an earlier one, or an agent question asked twice."""
    signals = []
    for role, threshold, who in (
        ("user", customer_overlap, "customer repeated"),
        ("agent", agent_overlap, "agent asked again"),
    ):
        hit = _first_repeat(conversation.turns, role, threshold, questions_only=role == "agent")
        if hit is not None:
            index, text = hit
            detail = f"{who}: {text[:120]!r}"
            signals.append(Signal(kind=SignalKind.REPEATED_QUESTION, turn=index, detail=detail))
    return signals


def _first_repeat(
    turns: Sequence[ConversationTurn], role: str, threshold: float, *, questions_only: bool
) -> tuple[int, str] | None:
    earlier: list[set[str]] = []
    for index, turn in enumerate(turns):
        if turn.role != role or (questions_only and "?" not in turn.text):
            continue
        words = content_words(turn.text)
        if len(words) < _MIN_CONTENT_WORDS:
            continue
        if any(_jaccard(words, before) >= threshold for before in earlier):
            return index, turn.text
        earlier.append(words)
    return None


# --- sentiment -----------------------------------------------------------------------------

# Words for why customers call ("broken", "problem", "cancel") are left out: they describe the
# request, not the mood.
# fmt: off
NEGATIVE_WORDS: Final = frozenset({
    "angry", "annoyed", "annoying", "awful", "bad", "complaint",
    "confused", "disappointed", "disappointing", "disgusting", "fed", "frustrated",
    "frustrating", "furious", "hate", "horrible", "incompetent", "joke",
    "lied", "mess", "nonsense", "outrageous", "pathetic", "poor", "rubbish",
    "ridiculous", "rude", "sick", "stupid", "terrible", "unacceptable", "unhappy", "upset",
    "useless", "waste", "worse", "worst", "wrong", "unbelievable", "seriously", "again",
})
# fmt: on
# fmt: off
POSITIVE_WORDS: Final = frozenset({
    "thanks", "thank", "great", "perfect", "excellent", "good", "awesome", "brilliant",
    "helpful", "happy", "appreciate", "wonderful", "fantastic", "lovely", "nice", "glad",
    "pleased", "sorted", "resolved", "works", "fine", "amazing",
})
# fmt: on
_TREND_POINTS: Final = 2
_NEGATIONS: Final = frozenset({"not", "no", "never", "isn't", "wasn't", "don't", "didn't"})


def sentiment(text: str) -> float:
    """Lexicon sentiment in [-1, 1]; a negation flips the next sentiment word."""
    score, hits, negate = 0, 0, False
    for word in _WORD.findall(text.casefold()):
        polarity = (word in POSITIVE_WORDS) - (word in NEGATIVE_WORDS)
        if polarity:
            score += -polarity if negate else polarity
            hits += 1
        negate = word in _NEGATIONS
    exclaims = text.count("!") if score < 0 else 0
    return max(-1.0, min(1.0, (score - 0.5 * min(exclaims, 2)) / (hits + 1)))


def negative_sentiment_trend(conversation: Conversation, *, drop: float = 0.3) -> list[Signal]:
    """The customer ends clearly negative, and worse than they started."""
    scores = [(i, sentiment(t.text)) for i, t in enumerate(conversation.turns) if t.role == "user"]
    if len(scores) < _TREND_POINTS:
        return []
    first = scores[0][1]
    worst_index, worst = min(scores[1:], key=lambda item: item[1])
    if worst <= -drop and worst <= first - drop:
        detail = f"customer sentiment fell from {first:+.2f} to {worst:+.2f}"
        return [Signal(kind=SignalKind.NEGATIVE_SENTIMENT, turn=worst_index, detail=detail)]
    return []


# --- detection -----------------------------------------------------------------------------


def heuristic_signals(conversation: Conversation) -> list[Signal]:
    """Every heuristic's signals, in a fixed order."""
    return [
        *escalation(conversation),
        *abandonment(conversation),
        *repeated_questions(conversation),
        *negative_sentiment_trend(conversation),
    ]


def describe(conversation: Conversation, signals: Sequence[Signal], max_chars: int = 1500) -> str:
    """A plain-text failure summary: the signals, then what each side said."""
    customer = " / ".join(t.text for t in conversation.turns if t.role == "user")
    agent = " / ".join(t.text for t in conversation.turns if t.role == "agent" and t.text)
    kinds = ", ".join(dict.fromkeys(signal.kind.value for signal in signals))
    return f"Failure signals: {kinds}. Customer: {customer} Agent: {agent}"[:max_chars]


async def detect_failures(
    conversations: Sequence[Conversation],
    *,
    llm: LLMProvider | None = None,
    judge_model: str | None = None,
    rubrics: Sequence[str] = (),
    seed: int = 0,
    concurrency: int = 8,
) -> list[FailureReport]:
    """One report per conversation, in input order.

    Judges run when ``rubrics`` are given. A failed verdict is a ``judge`` signal; a judge that
    reached no verdict is listed in ``judge_errors`` instead.

    Raises:
        ValueError: ``rubrics`` are given without a model to run them.
    """
    if rubrics and (llm is None or judge_model is None):
        raise ValueError("judge rubrics need an LLM and a judge model")
    reports: dict[int, FailureReport] = {}
    limiter = anyio.CapacityLimiter(concurrency)

    async def detect(index: int, conversation: Conversation) -> None:
        signals = heuristic_signals(conversation)
        errors: list[str] = []
        if rubrics and llm is not None and judge_model is not None:
            async with limiter:
                grades = await judge_conversation(
                    conversation, rubrics, llm, judge_model, seed=seed
                )
            signals += [
                Signal(kind=SignalKind.JUDGE, detail=f"{g.grader}: {g.rationale or 'failed'}")
                for g in grades
                if g.status == "ok" and not g.passed
            ]
            errors = [f"{g.grader}: {g.rationale}" for g in grades if g.status == "error"]
        reports[index] = FailureReport(
            conversation_id=conversation.id,
            signals=signals,
            judge_errors=errors,
            summary=describe(conversation, signals) if signals else "",
        )

    async with anyio.create_task_group() as group:
        for index, conversation in enumerate(conversations):
            group.start_soon(detect, index, conversation)
    return [reports[index] for index in range(len(conversations))]
