"""Reply builders for driving the simulator with ``FakeLLM`` (unit tests, the E2E fake pipeline).

Example::

    llm = FakeLLM(script=[decision_reply("Hi, I need to move my visit."),
                          decision_reply("Thanks!", action="end", end_reason="goal_achieved")])
"""

from collections.abc import Sequence
from typing import Literal

from hecklr_sim.decision import ConsistencyVerdict, LLMEndReason, SimDecision


def decision_reply(
    text: str,
    *,
    action: Literal["speak", "end"] = "speak",
    end_reason: LLMEndReason | None = None,
    facts_revealed: Sequence[str] = (),
    progress: bool = True,
    subject: str = "",
) -> SimDecision:
    """A ``SimDecision`` as the simulator's LLM would return it."""
    return SimDecision.model_validate(
        {
            "action": action,
            "text": text,
            "end_reason": end_reason,
            "facts_revealed": list(facts_revealed),
            "agent_made_progress": progress,
            "subject": subject,
        }
    )


def verdict_reply(*, consistent: bool, issue: str = "") -> ConsistencyVerdict:
    """A ``ConsistencyVerdict`` as the optional LLM consistency check would return it."""
    return ConsistencyVerdict(consistent=consistent, issue=issue)
