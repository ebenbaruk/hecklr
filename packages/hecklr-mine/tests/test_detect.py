"""Stage 3 (WS13 detect): heuristics and hecklr_grade judges flag failed conversations."""

import json
import re
from typing import Any

import anyio
import pytest

from hecklr_llm import FakeLLM
from hecklr_mine.detect import (
    FailureReport,
    SignalKind,
    abandonment,
    describe,
    detect_failures,
    escalation,
    heuristic_signals,
    negative_sentiment_trend,
    repeated_questions,
    sentiment,
)
from hecklr_mine.judges import conversation_events, grade_context, stable_ulid
from hecklr_schemas import Conversation, LLMRequest, TrialId


def _conversation(*turns: tuple[str, str], cid: str = "c1", **extra: Any) -> Conversation:
    return Conversation.model_validate(
        {"id": cid, "turns": [{"role": r, "text": t} for r, t in turns], **extra}
    )


HAPPY = _conversation(
    ("user", "Hi, can I move my boiler visit to Friday?"),
    ("agent", "Sure, Friday 9am is free. Shall I book it?"),
    ("user", "Yes please"),
    ("agent", "Done, see you Friday."),
    ("user", "Thanks, great!"),
)


def test_ws13_a_clean_conversation_has_no_signal() -> None:
    assert heuristic_signals(HAPPY) == []


@pytest.mark.parametrize(
    ("turns", "metadata", "turn"),
    [
        ((("user", "I want to speak to a real person"),), {}, 0),
        ((("user", "hi"), ("agent", "I'll transfer you to a human colleague now.")), {}, 1),
        ((("user", "Manager, please!"),), {}, 0),
        ((("user", "hi"),), {"escalated": True}, None),
        ((("user", "hi"),), {"outcome": "Handoff"}, None),
    ],
)
def test_ws13_escalation(
    turns: tuple[tuple[str, str], ...], metadata: dict[str, Any], turn: int | None
) -> None:
    [signal] = escalation(_conversation(*turns, metadata=metadata))
    assert signal.kind is SignalKind.ESCALATION
    assert signal.turn == turn


@pytest.mark.parametrize(
    ("turns", "metadata"),
    [
        ((("user", "Where is my refund?"), ("agent", "Checking."), ("user", "Forget it.")), {}),
        ((("user", "Hello?"), ("agent", "Hi"), ("user", "My heater broke, help")), {}),
        ((("user", "hi"),), {"status": "abandoned"}),
    ],
)
def test_ws13_abandonment(turns: tuple[tuple[str, str], ...], metadata: dict[str, Any]) -> None:
    [signal] = abandonment(_conversation(*turns, metadata=metadata))
    assert signal.kind is SignalKind.ABANDONMENT


def test_ws13_a_closing_thanks_or_single_message_is_not_abandonment() -> None:
    assert (
        abandonment(_conversation(("user", "Hi"), ("agent", "Hello"), ("user", "ok thanks"))) == []
    )
    assert abandonment(_conversation(("user", "Is anyone there?"))) == []


def test_ws13_repeated_questions_by_customer_and_agent() -> None:
    conversation = _conversation(
        ("user", "I need the refund for invoice number 4471"),
        ("agent", "Could you give me your invoice number please?"),
        ("user", "I said: the refund for invoice number 4471"),
        ("agent", "Could you give me the invoice number please?"),
    )
    signals = repeated_questions(conversation)
    assert [(s.kind, s.turn) for s in signals] == [
        (SignalKind.REPEATED_QUESTION, 2),
        (SignalKind.REPEATED_QUESTION, 3),
    ]
    assert "customer repeated" in signals[0].detail
    assert "agent asked again" in signals[1].detail


def test_ws13_short_messages_never_count_as_repeats() -> None:
    conversation = _conversation(
        ("user", "yes"), ("agent", "ok?"), ("user", "yes"), ("agent", "ok?")
    )
    assert repeated_questions(conversation) == []


def test_ws13_sentiment_lexicon_and_negation() -> None:
    assert sentiment("Thanks, that's great") > 0
    assert sentiment("This is terrible and useless!!") <= -0.6
    assert sentiment("not bad at all") > 0
    assert sentiment("") == 0


def test_ws13_negative_sentiment_trend() -> None:
    worsening = _conversation(
        ("user", "Hello, I'd like to book a visit"),
        ("agent", "Booked for the wrong day."),
        ("user", "That's wrong. This is ridiculous and unacceptable!"),
    )
    [signal] = negative_sentiment_trend(worsening)
    assert signal.turn == 2
    always_grumpy = _conversation(("user", "terrible service"), ("user", "still terrible"))
    assert negative_sentiment_trend(always_grumpy) == []
    assert negative_sentiment_trend(_conversation(("user", "awful"))) == []


def test_ws13_summary_lists_signal_kinds_and_both_sides() -> None:
    conversation = _conversation(("user", "Forget it"), ("agent", "Sorry"))
    signals = heuristic_signals(conversation)
    summary = describe(conversation, signals)
    assert summary.startswith("Failure signals: abandonment.")
    assert "Customer: Forget it" in summary
    assert "Agent: Sorry" in summary


# --- judges --------------------------------------------------------------------------------


def _tool_conversation() -> Conversation:
    return Conversation.model_validate(
        {
            "id": "prod-1",
            "turns": [
                {"role": "user", "text": "Move my visit", "t_ms": 0},
                {
                    "role": "agent",
                    "text": "Moved.",
                    "t_ms": 1500,
                    "tool_calls": [
                        {"tool": "reschedule", "args": {"day": "fri"}, "result": {"ok": True}},
                        {"tool": "notify", "result": "x", "error": "smtp down"},
                    ],
                },
            ],
        }
    )


def test_ws13_production_conversation_becomes_judgeable_trial_events() -> None:
    conversation = _tool_conversation()
    trial_id = TrialId(stable_ulid("prod-1", "trial"))
    events = conversation_events(conversation, trial_id)
    assert [e.kind for e in events] == [
        "utterance",
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
        "utterance",
    ]
    assert events[4].payload.kind == "tool_result"
    assert events[4].payload.error == "smtp down"
    assert events[4].payload.result is None
    # Deterministic ids: the judge prompt and therefore the response cache key are stable.
    assert conversation_events(conversation, trial_id) == events
    ctx = grade_context(conversation, FakeLLM(), "fake/judge", seed=1)
    assert ctx.trial.duration_ms == 1500
    assert ctx.scenario.goal.startswith("Unknown")


def _judge_reply(verdict: str) -> Any:
    def responder(request: LLMRequest) -> dict[str, Any]:
        ids = re.findall(r"^\[([0-9A-Z]{26})\]", request.messages[-1].content, re.MULTILINE)
        return {"verdict": verdict, "rationale": "the visit was never moved", "evidence": ids[:1]}

    return responder


def test_ws13_judges_add_a_signal_when_they_fail_the_conversation() -> None:
    llm = FakeLLM(responder=_judge_reply("fail"))
    [report] = anyio.run(
        lambda: detect_failures(
            [HAPPY], llm=llm, judge_model="fake/judge", rubrics=["resolved"], seed=4
        )
    )
    assert [s.kind for s in report.signals] == [SignalKind.JUDGE]
    assert report.signals[0].detail == "judge:resolved: the visit was never moved"
    assert report.failed
    assert report.summary.startswith("Failure signals: judge.")
    # A production conversation's world is unknown: the judge must not see an empty one as truth.
    prompt = llm.calls[0].messages[0].content
    assert "# World state" not in prompt
    assert "# Transcript" in prompt


def test_ws13_passing_and_erroring_judges_add_no_signal_but_errors_are_kept() -> None:
    passing = anyio.run(
        lambda: detect_failures(
            [HAPPY],
            llm=FakeLLM(responder=_judge_reply("pass")),
            judge_model="fake/j",
            rubrics=["resolved"],
        )
    )
    assert passing == [FailureReport(conversation_id="c1")]
    garbage = anyio.run(
        lambda: detect_failures(
            [HAPPY], llm=FakeLLM(script=["not json"]), judge_model="fake/j", rubrics=["tone"]
        )
    )
    assert not garbage[0].failed
    assert garbage[0].judge_errors
    assert garbage[0].judge_errors[0].startswith("judge:tone")


def test_ws13_rubrics_need_a_model() -> None:
    with pytest.raises(ValueError, match="need an LLM"):
        anyio.run(lambda: detect_failures([HAPPY], rubrics=["resolved"]))


def test_ws13_detection_without_judges_keeps_input_order() -> None:
    sad = _conversation(("user", "speak to a human now"), cid="c2")
    reports = anyio.run(lambda: detect_failures([HAPPY, sad]))
    assert [r.conversation_id for r in reports] == ["c1", "c2"]
    assert [r.failed for r in reports] == [False, True]
    json.loads(reports[1].model_dump_json())
