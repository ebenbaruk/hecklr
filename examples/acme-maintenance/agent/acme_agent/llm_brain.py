"""The LLM brain: the reference agent's real brain, a tool-calling loop over ``hecklr_llm``.

``hecklr_llm``'s request has no native tool-calling field, so each step is one structured answer
(``AgentStep``): call a tool, or reply to the customer. The model sees the system prompt, the
tool catalog and the whole conversation so far, including earlier tool calls and their results.
"""

import hashlib
import json
from typing import Final, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from acme_agent.config import AgentFiles, AgentSettings
from acme_agent.tools import ToolRunner
from hecklr_llm import complete_structured
from hecklr_schemas import LLMProvider, LLMRequest, Message

_log = structlog.get_logger(__name__)

STEP_PROTOCOL: Final = """\
How to answer: reply with exactly one JSON object per step.
- To call a tool: {"action": "call_tool", "tool": "<tool name>", "arguments": {...}, "message": ""}
- To answer the customer: {"action": "reply", "tool": "", "arguments": {}, "message": "<text>"}
Call one tool at a time; you will see its result before your next step. Never invent a tool
result: if you need a fact, call the tool. When you reply, the customer sees only "message"."""

GAVE_UP: Final = "I'm sorry, I couldn't finish that just now. Could you say it again?"
MAX_TOKENS: Final = 1024


class AgentStep(BaseModel):
    """One step of the loop, as the model answers it.

    Every field is required on purpose: small models fill required fields reliably.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["call_tool", "reply"]
    tool: str = Field(description="Tool name when calling a tool, otherwise empty.")
    arguments: dict[str, str | int | float | bool] = Field(
        description="Tool arguments when calling a tool, otherwise empty."
    )
    message: str = Field(description="Text for the customer when replying, otherwise empty.")


def _argument(name: str, spec: JsonValue, required: bool) -> str:
    spec = spec if isinstance(spec, dict) else {}
    enum = spec.get("enum")
    kind = "one of " + "/".join(map(str, enum)) if isinstance(enum, list) else spec.get("type")
    text = f"{name} ({kind or 'any'}{', required' if required else ''})"
    note = spec.get("description")
    return f"{text}: {note.rstrip('.')}" if isinstance(note, str) else text


def render_catalog(files: AgentFiles) -> str:
    """The tool catalog as the model reads it: one line per tool, arguments described in prose.

    Prose, not raw JSON Schema: it is about a third of the tokens, and local models often run
    with a 4k-token context that the prompt, the conversation and tool results must share.
    """
    lines = []
    for spec in files.tools.values():
        properties = spec.parameters.get("properties")
        required = spec.parameters.get("required")
        needed = set(required) if isinstance(required, list) else set()
        arguments = "; ".join(
            _argument(name, value, name in needed)
            for name, value in (properties.items() if isinstance(properties, dict) else [])
        )
        lines.append(f"- {spec.name}: {spec.description} Arguments: {arguments or 'none'}.")
    return "\n".join(lines)


def system_prompt(files: AgentFiles, settings: AgentSettings) -> str:
    """System prompt file + today's date + tool catalog + the step protocol."""
    today = settings.today
    return (
        f"{files.system_prompt.strip()}\n\n"
        f"Today is {today:%A} {today.day} {today:%B %Y}.\n\n"
        f"Tools:\n{render_catalog(files)}\n\n{STEP_PROTOCOL}"
    )


def step_seed(base: int, turn: int, step: int) -> int:
    """A request seed fixed by the conversation position, so a replayed turn asks the same."""
    digest = hashlib.blake2b(f"{base}:{turn}:{step}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") % (2**31 - 1)


class LLMBrain:
    """One conversation's LLM brain; its memory is the transcript it renders every step."""

    def __init__(self, files: AgentFiles, settings: AgentSettings, llm: LLMProvider) -> None:
        self._settings = settings
        self._llm = llm
        self._system = system_prompt(files, settings)
        self._transcript: list[str] = []
        self._turn = 0

    async def reply(self, message: str, tools: ToolRunner) -> str:
        """Run tool steps until the model replies, at most ``settings.max_steps`` steps.

        Raises:
            hecklr_llm.LLMError: the model failed or answered malformed JSON; the server turns
                this into an HTTP 500, which Hecklr records as an agent error.
        """
        self._turn += 1
        self._transcript.append(f"Customer: {message}")
        for step in range(self._settings.max_steps):
            decision = await self._decide(step)
            if decision.action == "reply" or not decision.tool:
                text = decision.message.strip() or GAVE_UP
                self._transcript.append(f"You: {text}")
                return text
            outcome = await tools.call(decision.tool, decision.arguments)
            self._transcript.append(
                f"Tool call: {decision.tool} "
                f"{json.dumps(decision.arguments, ensure_ascii=False, separators=(',', ':'))}"
            )
            result = json.dumps(outcome.body, ensure_ascii=False, separators=(",", ":"))
            self._transcript.append(f"Tool result: {result}")
        _log.warning("llm_brain_step_limit", steps=self._settings.max_steps, turn=self._turn)
        self._transcript.append(f"You: {GAVE_UP}")
        return GAVE_UP

    async def _decide(self, step: int) -> AgentStep:
        conversation = "\n".join(self._transcript)
        request = LLMRequest(
            model=self._settings.model,
            system=self._system,
            messages=[
                Message(
                    role="user",
                    content=f"Conversation so far:\n{conversation}\n\nDecide your next step.",
                )
            ],
            temperature=self._settings.temperature,
            max_tokens=MAX_TOKENS,
            seed=step_seed(self._settings.seed, self._turn, step),
        )
        decision, _ = await complete_structured(self._llm, request, AgentStep)
        return decision
