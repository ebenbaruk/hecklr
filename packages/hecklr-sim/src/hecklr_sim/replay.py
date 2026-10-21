"""Replay extraction (US-02): real transcripts become adaptive ``replay`` scenarios.

An LLM reads a logged ``Conversation`` and extracts what the customer wanted (goal) and what it
knew (facts). The scenario then runs as ``guided``: the simulated customer pursues the same goal
against the new agent instead of replaying the old customer's exact words.

Facts are grounded: a value the customer never actually wrote is dropped, because the extractor
may hallucinate details and the simulator would then "know" something the real customer did not.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import anyio
import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from hecklr_llm import LLMOutputError, complete_structured
from hecklr_schemas import (
    Conversation,
    JudgeCriterion,
    LLMProvider,
    LLMRequest,
    Message,
    Scenario,
    ScenarioLimits,
    ScenarioSource,
    SimulationMode,
    ToolCalledCriterion,
)
from hecklr_schemas.scenario import SuccessCriterion
from hecklr_sim.errors import ReplayExtractionError
from hecklr_sim.seeds import derive_seed
from hecklr_sim.tokens import mentions

_EXTRACTOR_SYSTEM = (
    "You turn a logged customer-support conversation into a test scenario for a customer "
    "simulator. Extract, from the customer's point of view:\n"
    "- title: a short title of this customer's situation, starting with 'Customer'.\n"
    "- goal: what the customer wanted to achieve, in one or two sentences, including "
    "constraints they stated (dates, preferences). Do not describe what the agent did.\n"
    "- facts: every concrete detail the customer gave (identifiers, postcodes, dates, names, "
    "amounts), as snake_case names with the value exactly as the customer wrote it. Only "
    "details the customer said, never ones only the agent said.\n"
    "- resolving_tool: the name of the agent tool call that fulfilled the goal, or null.\n"
    "- tags: one to three short lowercase topic tags."
)
_SLUG = re.compile(r"[^a-z0-9._-]+")
_MAX_ID = 128
_MAX_TAGS = 3
"""Topic tags kept from the extractor, besides ``replay``; models tend to over-tag."""
_MIN_FACT_LENGTH = 2
"""Shorter values ("1", "a") occur in almost any text, so their grounding proves nothing."""


class ExtractedFact(BaseModel):
    """One detail the customer gave, as extracted."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="snake_case name, e.g. customer_id.")
    value: str = Field(description="The value exactly as the customer wrote it.")


class ReplayExtraction(BaseModel):
    """What the extractor LLM returns for one conversation."""

    model_config = ConfigDict(extra="forbid")

    title: str
    goal: str
    facts: list[ExtractedFact]
    resolving_tool: str | None
    tags: list[str]


async def extract_scenario(
    conversation: Conversation,
    llm: LLMProvider,
    *,
    model: str,
    world: str,
    persona: str,
    seed: int = 0,
) -> Scenario:
    """Extract a ``replay`` scenario from one real conversation.

    Args:
        conversation: The logged conversation.
        llm: Provider used for the extraction call.
        model: Extractor model, ``provider/model``.
        world: World id the scenario runs against.
        persona: Persona id(s) to cross it with; replay keeps the goal, not the original speaker.
        seed: Run seed; each conversation's request seed derives from it and the conversation id,
            so re-extracting an unchanged file hits the response cache.

    Raises:
        hecklr_llm.LLMOutputError: The model's reply did not match ``ReplayExtraction``.
        ReplayExtractionError: The extraction is unusable (an empty goal).
    """
    request = LLMRequest(
        model=model,
        system=_EXTRACTOR_SYSTEM,
        messages=[Message(role="user", content=_render_conversation(conversation))],
        max_tokens=1024,
        seed=derive_seed(seed, "replay", conversation.id),
    )
    extraction, _ = await complete_structured(llm, request, ReplayExtraction)
    if not extraction.goal.strip():
        raise ReplayExtractionError(
            f"conversation {conversation.id!r}: the extractor returned no goal"
        )
    return Scenario(
        id=scenario_id_for(conversation.id),
        title=extraction.title.strip() or f"Replay of {conversation.id}",
        channels=[conversation.channel],
        tags=_tags(extraction.tags),
        persona=persona,
        world=world,
        goal=extraction.goal.strip(),
        facts=grounded_facts(extraction.facts, conversation),
        success=_success_criteria(extraction.resolving_tool, conversation),
        limits=ScenarioLimits(
            max_turns=max(ScenarioLimits().max_turns, 2 * len(conversation.turns))
        ),
        mode=SimulationMode.REPLAY,
        source=ScenarioSource(kind="replay", conversation_id=conversation.id),
    )


def scenario_id_for(conversation_id: str) -> str:
    """The scenario id for a conversation: ``replay-`` plus a slug of its id."""
    slug = _SLUG.sub("-", conversation_id.lower()).strip("-._") or "conversation"
    return f"replay-{slug}"[:_MAX_ID]


def grounded_facts(
    facts: Sequence[ExtractedFact], conversation: Conversation
) -> dict[str, JsonValue]:
    """Keep facts whose value the customer actually wrote; normalize names to snake_case.

    A value counts as written only as a whole token sequence (``C-26`` is not grounded by
    ``C-2675``) and only if it has at least two characters. The first fact wins when two
    normalize to the same name.
    """
    customer_text = " ".join(t.text for t in conversation.turns if t.role == "user").casefold()
    kept: dict[str, JsonValue] = {}
    for fact in facts:
        name = re.sub(r"[^a-z0-9]+", "_", fact.name.lower()).strip("_")
        value = fact.value.strip()
        if (
            name
            and len(value) >= _MIN_FACT_LENGTH
            and name not in kept
            and mentions(customer_text, value.casefold())
        ):
            kept[name] = value
    return kept


def load_conversations(path: Path) -> list[Conversation]:
    """Read a native JSONL transcript file: one ``Conversation`` per non-blank line.

    Raises:
        ValueError: A line is not a valid conversation; the message names the line number.
    """
    conversations = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                conversations.append(Conversation.model_validate_json(line))
            except ValidationError as exc:
                raise ValueError(f"{path}:{number}: not a valid conversation: {exc}") from exc
    return conversations


def scenario_to_yaml(scenario: Scenario) -> str:
    """Serialize a scenario as authored YAML (defaults omitted, field order kept)."""
    data = scenario.model_dump(mode="json", exclude_defaults=True)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


@dataclass(frozen=True)
class ReplayFailure:
    """A conversation whose extraction failed; the rest of the batch is still written."""

    conversation_id: str
    error: str


@dataclass(frozen=True)
class ReplayBatch:
    """What ``transcripts_to_scenarios`` did: the files written and the conversations skipped."""

    written: list[Path]
    failures: list[ReplayFailure]


async def transcripts_to_scenarios(
    path: Path,
    out_dir: Path,
    llm: LLMProvider,
    *,
    model: str,
    world: str,
    persona: str,
    seed: int = 0,
    concurrency: int = 8,
) -> ReplayBatch:
    """Convert a JSONL transcript file into replay scenario YAML files (``hecklr replay FILE``).

    One file per conversation, ``<out_dir>/<scenario id>.yaml``, in input order. A conversation
    whose extraction fails (an invalid model reply, an empty goal) is reported in
    ``ReplayBatch.failures`` instead of losing the whole batch. Existing files are never
    overwritten: scenarios are authored artifacts that people edit after extraction.

    Raises:
        ValueError: Invalid input, or two conversations map to the same scenario id.
        FileExistsError: A scenario file already exists.
    """
    conversations = await anyio.to_thread.run_sync(load_conversations, path)
    _check_unique_ids(conversations)
    outcomes: dict[int, Scenario | ReplayFailure] = {}
    limiter = anyio.CapacityLimiter(concurrency)

    async def extract(index: int, conversation: Conversation) -> None:
        async with limiter:
            try:
                outcomes[index] = await extract_scenario(
                    conversation, llm, model=model, world=world, persona=persona, seed=seed
                )
            except (LLMOutputError, ReplayExtractionError) as exc:
                outcomes[index] = ReplayFailure(conversation.id, str(exc))

    async with anyio.create_task_group() as group:
        for index, conversation in enumerate(conversations):
            group.start_soon(extract, index, conversation)
    ordered = [outcomes[index] for index in range(len(conversations))]
    scenarios = [outcome for outcome in ordered if isinstance(outcome, Scenario)]
    failures = [outcome for outcome in ordered if isinstance(outcome, ReplayFailure)]
    written = await anyio.to_thread.run_sync(_write_scenarios, scenarios, out_dir)
    return ReplayBatch(written=written, failures=failures)


def _check_unique_ids(conversations: Sequence[Conversation]) -> None:
    seen: dict[str, str] = {}
    for conversation in conversations:
        scenario_id = scenario_id_for(conversation.id)
        if scenario_id in seen:
            raise ValueError(
                f"conversations {seen[scenario_id]!r} and {conversation.id!r} both map to "
                f"scenario id {scenario_id!r}"
            )
        seen[scenario_id] = conversation.id


def _write_scenarios(scenarios: Sequence[Scenario], out_dir: Path) -> list[Path]:
    paths = [out_dir / f"{scenario.id}.yaml" for scenario in scenarios]
    # Refuse before writing anything, so a clash never leaves a half-written batch behind.
    existing = [str(target) for target in paths if target.exists()]
    if existing:
        raise FileExistsError(f"refusing to overwrite scenario file(s): {', '.join(existing)}")
    out_dir.mkdir(parents=True, exist_ok=True)
    for scenario, target in zip(scenarios, paths, strict=True):
        with target.open("x", encoding="utf-8") as handle:
            handle.write(scenario_to_yaml(scenario))
    return paths


def _render_conversation(conversation: Conversation) -> str:
    lines = [f"Conversation {conversation.id} ({conversation.channel.value}):"]
    for turn in conversation.turns:
        speaker = "CUSTOMER" if turn.role == "user" else "AGENT"
        lines.append(f"{speaker}: {turn.text}")
        lines.extend(f"  [agent called tool {call.tool}({call.args})]" for call in turn.tool_calls)
    return "\n".join(lines)


def _tags(raw: Sequence[str]) -> list[str]:
    tags = ["replay"]
    for tag in raw:
        slug = _SLUG.sub("-", tag.lower()).strip("-._")[:_MAX_ID]
        if slug and slug not in tags and len(tags) <= _MAX_TAGS:
            tags.append(slug)
    return tags


def _success_criteria(
    resolving_tool: str | None, conversation: Conversation
) -> list[SuccessCriterion]:
    """A minimal starting point for authors: the ``resolved`` judge, plus the resolving tool.

    The tool criterion is added only if that tool was really called in the transcript.
    """
    criteria: list[SuccessCriterion] = [JudgeCriterion(type="judge", rubric="resolved")]
    called = {call.tool for turn in conversation.turns for call in turn.tool_calls}
    if resolving_tool is not None and resolving_tool in called:
        criteria.insert(0, ToolCalledCriterion(type="tool_called", tool=resolving_tool))
    return criteria
