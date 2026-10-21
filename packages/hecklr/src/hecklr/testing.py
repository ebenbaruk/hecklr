"""Deterministic models for running a whole Hecklr project offline, without any LLM.

``scripted_models()`` returns providers for the ``fake/`` model prefix: a simulated customer that
states its goal, answers the facts the agent asks for and then leaves satisfied, and a judge whose
verdict a test chooses. Use it to smoke-test an agent's wiring in CI, or in Hecklr's own tests::

    results = await hecklr.run_async("hecklr.yaml", suite="ci", llm_providers=scripted_models())

with ``models: {simulator: fake/customer, judge: fake/judge}`` in ``hecklr.yaml``. It exercises
the plumbing, not the agent's quality: the customer never pushes back.
"""

import re
from collections.abc import Callable

from hecklr_llm import FakeLLM, FakeReply
from hecklr_schemas import LLMProvider, LLMRequest
from hecklr_sim.testing import decision_reply, verdict_reply

JudgeRule = Callable[[LLMRequest], bool]
"""Decides a judge verdict from the judge's request (rubric, scenario and transcript are in it)."""

_FACT_LINE = re.compile(r"^- (\w+): (.+)$", re.MULTILINE)


def scripted_models(judge_passes: JudgeRule | None = None) -> dict[str, LLMProvider]:
    """Providers for ``fake/...`` models: a cooperative customer and a rule-driven judge.

    Args:
        judge_passes: Verdict of each judge call; every judge passes if ``None``.
    """

    def respond(request: LLMRequest) -> FakeReply:
        match request.schema_name:
            case "SimDecision":
                return _customer_move(request)
            case "ConsistencyVerdict":
                return verdict_reply(consistent=True)
            case "JudgeVerdict":
                passed = judge_passes is None or judge_passes(request)
                return {
                    "verdict": "pass" if passed else "fail",
                    "rationale": "Scripted verdict.",
                    "evidence": [],
                }
            case _:
                return f"Scripted reply to a {request.schema_name} request."

    return {"fake": FakeLLM(responder=respond)}


def _customer_move(request: LLMRequest) -> FakeReply:
    brief = request.system or ""
    goal = _section(brief, "## Your goal").splitlines()[0]
    facts = dict(_FACT_LINE.findall(_section(brief, "## Facts you know")))
    transcript = _section(request.messages[-1].content, "## Conversation so far").splitlines()
    said = " ".join(line for line in transcript if line.startswith("YOU:"))
    if not said:
        return decision_reply(f"Hi. {goal}")
    last_agent = next((line for line in reversed(transcript) if line.startswith("AGENT:")), "")
    for name, value in facts.items():
        if name.replace("_", " ") in last_agent.lower() and value not in said:
            return decision_reply(f"My {name.replace('_', ' ')} is {value}.", facts_revealed=[name])
    return decision_reply("Thanks, that's all.", action="end", end_reason="goal_achieved")


def _section(text: str, header: str) -> str:
    start = text.find(header)
    if start < 0:
        return ""
    body = text[start + len(header) :].lstrip("\n")
    end = body.find("\n## ")
    return body if end < 0 else body[:end]
