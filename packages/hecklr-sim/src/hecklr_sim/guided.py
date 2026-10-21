"""Guided mode: an LLM customer pursues the scenario's goal adaptively.

Replay scenarios run the same way (their goal and facts were extracted from a real transcript),
and so do adversarial personas, whose brief adds a hidden attack strategy.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from hecklr_llm import LLMOutputError, complete_structured
from hecklr_schemas import (
    Channel,
    Clock,
    DialogueTurn,
    EndReason,
    LLMProvider,
    LLMRequest,
    Message,
    SimEnd,
    SimTurn,
    Traits,
    TrialBrief,
)
from hecklr_sim.brief import render_brief, render_fact_value, render_turn_prompt
from hecklr_sim.decision import ConsistencyVerdict, SimDecision
from hecklr_sim.errors import SimulatorError
from hecklr_sim.guard import ConsistencyGuard, GuardIssue
from hecklr_sim.limits import limit_reached, user_turn_count
from hecklr_sim.seeds import derive_seed, unit_draw
from hecklr_sim.strategies import AttackStrategy

_CHECKER_SYSTEM = (
    "You audit a simulated customer. Given the customer's private brief and its drafted next "
    "message, decide whether the draft contradicts the brief: pursuing a different goal, giving "
    "fact values that differ from the brief, or stating identifiers or personal details the "
    "brief does not contain. Asking questions, expressing emotions, declining to share and "
    "following the conversation naturally are all consistent."
)


@dataclass(frozen=True)
class SimulatorSettings:
    """How LLM-driven simulators call their model; one instance serves a whole run."""

    model: str
    """``provider/model`` string, e.g. ``models.simulator`` from ``hecklr.yaml``."""
    max_regenerations: int = 2
    """Extra drafts allowed per turn after a rejected one; then ``SimulatorError``."""
    llm_consistency_check: bool = False
    """Also ask the model whether each draft contradicts the brief (catches a changed goal)."""
    temperature: float | None = None
    max_tokens: int = 512

    def __post_init__(self) -> None:
        if self.max_regenerations < 0:
            raise ValueError(f"max_regenerations must be >= 0, got {self.max_regenerations}")


@dataclass
class Patience:
    """How many agent messages in a row without progress the customer tolerates.

    Derived from ``traits.patience`` (0 → 1 message, 1 → 6) plus a seeded coin flip of one more,
    so two customers with the same traits do not all give up at exactly the same moment.
    """

    tolerance: int
    strikes: int = 0

    @classmethod
    def for_persona(cls, traits: Traits, seed: int) -> "Patience":
        """The patience of a persona with ``traits`` in the trial with ``seed``."""
        jitter = 1 if unit_draw(seed, "patience") < 0.5 else 0
        return cls(tolerance=1 + math.floor(traits.patience * 5) + jitter)

    @property
    def left(self) -> int:
        """Unproductive agent messages still tolerated."""
        return self.tolerance - self.strikes

    @property
    def exhausted(self) -> bool:
        """Whether the customer has run out of patience."""
        return self.strikes >= self.tolerance

    def record(self, *, progress: bool) -> None:
        """Count one agent message; progress resets the streak."""
        self.strikes = 0 if progress else self.strikes + 1


class GuidedSimulator:
    """A ``CustomerSimulator`` driven by an LLM with a private brief."""

    def __init__(
        self,
        brief: TrialBrief,
        llm: LLMProvider,
        settings: SimulatorSettings,
        clock: Clock,
        strategy: AttackStrategy | None = None,
    ) -> None:
        self._brief = brief
        self._llm = llm
        self._settings = settings
        self._clock = clock
        self._start_ms = clock.now_ms()
        self._system = render_brief(brief.scenario, brief.persona, brief.channel, strategy)
        self._strategy = strategy
        self._guard = ConsistencyGuard(brief.scenario, strategy)
        self._patience = Patience.for_persona(brief.persona.traits, brief.seed)
        self._contradictions = 0
        self._issues: list[GuardIssue] = []

    @property
    def contradictions(self) -> int:
        """Turns whose first draft the guard rejected and that had to be regenerated."""
        return self._contradictions

    @property
    def issues(self) -> list[GuardIssue]:
        """Every issue the guard raised so far, in order, for diagnostics."""
        return list(self._issues)

    @property
    def patience(self) -> Patience:
        """The customer's current patience (read-only use)."""
        return self._patience

    async def next_turn(self, dialogue: Sequence[DialogueTurn]) -> SimTurn | SimEnd:
        """Return the customer's next message, or end the conversation."""
        reason = limit_reached(
            dialogue, self._brief.scenario.limits, self._clock.now_ms() - self._start_ms
        )
        if reason is not None:
            return SimEnd(reason=reason)
        decision = await self._decide(dialogue)
        if _answers_agent(dialogue):
            self._patience.record(progress=decision.agent_made_progress)
        return self._to_message(decision, dialogue)

    def _to_message(
        self, decision: SimDecision, dialogue: Sequence[DialogueTurn]
    ) -> SimTurn | SimEnd:
        text = decision.text.strip()
        end_reason = decision.end_reason_enum()
        if end_reason is not None and decision.action == "end":
            return SimEnd(reason=end_reason, closing_text=text or None)
        if self._patience.exhausted:
            # The model was told it is out of patience but kept talking; the harness decides.
            return SimEnd(reason=EndReason.GOAL_ABANDONED, closing_text=text or None)
        first_email = self._brief.channel is Channel.EMAIL and user_turn_count(dialogue) == 0
        subject = (decision.subject.strip() or self._brief.scenario.title) if first_email else None
        return SimTurn(text=text, subject=subject)

    async def _decide(self, dialogue: Sequence[DialogueTurn]) -> SimDecision:
        turn = user_turn_count(dialogue)
        feedback: list[str] = []
        problems: list[str] = []
        regenerated = False
        attempts = self._settings.max_regenerations + 1
        for attempt in range(attempts):
            request = self._decision_request(dialogue, feedback, turn, attempt)
            try:
                decision, _ = await complete_structured(self._llm, request, SimDecision)
            except LLMOutputError as exc:
                feedback = [f"your answer was not a valid JSON object of the required shape: {exc}"]
                problems = feedback
                continue
            issues = self._guard.check(decision, dialogue)
            if not issues and self._settings.llm_consistency_check:
                try:
                    issues = await self._llm_check(decision, dialogue, turn, attempt)
                except LLMOutputError as exc:
                    # The draft is not at fault, so it gets no feedback and is not counted as a
                    # contradiction; an unaudited draft is not accepted either, so it costs a try.
                    feedback = []
                    problems = [f"the consistency check's reply was invalid: {exc}"]
                    continue
            if not issues:
                self._contradictions += int(regenerated)
                return decision
            regenerated = True
            self._issues.extend(issues)
            feedback = [issue.detail for issue in issues]
            problems = feedback
        raise SimulatorError(
            f"trial {self._brief.trial_id}, customer turn {turn}: no valid draft in {attempts} "
            f"attempt(s); last problems: {'; '.join(problems)}"
        )

    def _decision_request(
        self, dialogue: Sequence[DialogueTurn], feedback: Sequence[str], turn: int, attempt: int
    ) -> LLMRequest:
        prompt = render_turn_prompt(
            dialogue,
            patience_left=self._patience.left,
            turns_left=self._brief.scenario.limits.max_turns - len(dialogue),
            first_email=self._brief.channel is Channel.EMAIL and turn == 0,
            feedback=feedback,
        )
        return self._request(
            self._system, prompt, derive_seed(self._brief.seed, "turn", turn, attempt)
        )

    async def _llm_check(
        self, decision: SimDecision, dialogue: Sequence[DialogueTurn], turn: int, attempt: int
    ) -> list[GuardIssue]:
        facts = "\n".join(
            f"- {name}: {render_fact_value(value)}"
            for name, value in self._brief.scenario.facts.items()
        )
        decoys = self._strategy.decoys if self._strategy is not None else {}
        others = "".join(f"\n- {name}: {value}" for name, value in decoys.items())
        last_agent = next((t.text for t in reversed(dialogue) if t.speaker == "agent"), "(none)")
        prompt = (
            f"## Brief\nGoal: {self._brief.scenario.goal}\nFacts:\n{facts or '(none)'}\n"
            f"Other people's details it may legitimately mention:{others or ' (none)'}\n\n"
            f"## Latest agent message\n{last_agent}\n\n"
            f"## Draft to check\n{decision.text}"
        )
        seed = derive_seed(self._brief.seed, "check", turn, attempt)
        verdict, _ = await complete_structured(
            self._llm, self._request(_CHECKER_SYSTEM, prompt, seed), ConsistencyVerdict
        )
        if verdict.consistent:
            return []
        return [GuardIssue("llm_flagged", verdict.issue or "the message contradicts your brief")]

    def _request(self, system: str, prompt: str, seed: int) -> LLMRequest:
        return LLMRequest(
            model=self._settings.model,
            system=system,
            messages=[Message(role="user", content=prompt)],
            temperature=self._settings.temperature,
            max_tokens=self._settings.max_tokens,
            seed=seed,
        )


def _answers_agent(dialogue: Sequence[DialogueTurn]) -> bool:
    """Whether the customer is replying to an agent message that followed one of its own."""
    return bool(dialogue) and dialogue[-1].speaker == "agent" and user_turn_count(dialogue) > 0
