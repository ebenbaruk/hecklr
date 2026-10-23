"""Step 2: N candidate patches from a model that sees the failures and the editable files.

Each candidate is an independent request with its own derived seed (and its number in the
prompt), so candidates differ and each is cached on its own. A reply that is not a valid patch,
edits a file outside ``editable`` or does not apply is recorded as a rejected proposal with the
reason; it never reaches the agent.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from pydantic import ValidationError

from hecklr_fix.errors import PatchRejectedError
from hecklr_fix.patching import ProposedPatch, apply_edits, unified_diff
from hecklr_fix.target import FixTarget
from hecklr_fix.validation import SuiteRun
from hecklr_grade.judge import render_transcript
from hecklr_llm import LLMOutputError, LLMRefusalError
from hecklr_report.outcomes import convert
from hecklr_schemas import LLMProvider, LLMRequest, LLMResponse, Message
from hecklr_sim import derive_subseed

TRANSCRIPT_CHARS: Final = 6_000
"""Per failing trial; the end of a long conversation is kept, where failures usually show."""
MAX_TOKENS: Final = 8_192

SYSTEM_PROMPT: Final = """\
You repair the instructions of a customer-support AI agent. The agent's behaviour comes from a
few text files (its system prompt and tool descriptions). Simulated customers found a failure;
you see the failing conversations, why each one was graded as failing, and the current files.

Propose one minimal change to the files that makes the agent behave correctly in conversations
like these, without breaking anything else it does:
- Fix the root cause stated in general terms. Never mention the specific customers, ids, dates or
  amounts of these conversations: the change must work for every customer.
- Keep every other rule. Do not loosen safety, verification or privacy rules.
- Each edit replaces one passage of one file. "find" must be copied character for character from
  the current file and occur exactly once in it; "replace" is the new text for that passage.
- Only the files listed below may be edited.

Answer with JSON: {"rationale": "...", "edits": [{"file": "...", "find": "...", "replace": "..."}]}.
"""


@dataclass(frozen=True, slots=True)
class Candidate:
    """A proposal that applies cleanly to the editable files."""

    index: int
    rationale: str
    files: dict[str, str]
    """The new text of every file it changes."""
    diff: str
    model: str
    cost_usd: Decimal


@dataclass(frozen=True, slots=True)
class RejectedProposal:
    """A proposal dropped before any run, and why."""

    index: int
    reason: str


@dataclass(frozen=True, slots=True)
class Generation:
    """Everything step 2 produced."""

    candidates: list[Candidate]
    rejected: list[RejectedProposal]
    cost_usd: Decimal


async def generate_candidates(
    llm: LLMProvider,
    *,
    model: str,
    target: FixTarget,
    files: Mapping[str, str],
    evidence: str,
    count: int,
    seed: int,
) -> Generation:
    """Ask ``model`` for ``count`` independent patches.

    Raises:
        hecklr_llm.LLMError: the provider failed (anything but an unusable reply).
    """
    candidates: list[Candidate] = []
    rejected: list[RejectedProposal] = []
    seen: dict[str, int] = {}
    cost = Decimal(0)
    for index in range(count):
        request = LLMRequest(
            model=model,
            system=SYSTEM_PROMPT,
            messages=[Message(role="user", content=_prompt(target, files, evidence, index, count))],
            seed=derive_subseed(seed, "hecklr-fix", index),
            max_tokens=MAX_TOKENS,
            json_schema=ProposedPatch.model_json_schema(),
            schema_name="ProposedPatch",
        )
        try:
            response = await llm.complete(request)
        except (LLMOutputError, LLMRefusalError) as exc:
            rejected.append(RejectedProposal(index, f"unusable reply: {exc}"))
            continue
        # Counted before parsing: an unusable reply was still paid for.
        cost += response.cost_usd
        try:
            proposal = _parse(response)
            changed = apply_edits(files, proposal.edits)
        except PatchRejectedError as exc:
            rejected.append(RejectedProposal(index, str(exc)))
            continue
        diff = unified_diff(files, changed)
        if diff in seen:
            rejected.append(RejectedProposal(index, f"same patch as candidate {seen[diff]}"))
            continue
        seen[diff] = index
        candidates.append(
            Candidate(index, proposal.rationale, changed, diff, response.model, response.cost_usd)
        )
    return Generation(candidates, rejected, cost)


def _parse(response: LLMResponse) -> ProposedPatch:
    if response.truncated:
        raise PatchRejectedError(f"the reply was cut off at max_tokens={MAX_TOKENS}")
    try:
        return ProposedPatch.model_validate_json(response.text)
    except ValidationError as exc:
        raise PatchRejectedError(
            f"the reply is not a patch ({exc.error_count()} error(s)): {response.text[:200]!r}"
        ) from exc


def _prompt(
    target: FixTarget, files: Mapping[str, str], evidence: str, index: int, count: int
) -> str:
    cluster = target.cluster
    sections = [
        f"# Failure\n\n{cluster.label}"
        + (f"\n\n{cluster.description}" if cluster.description else ""),
        f"# Failing conversations\n\n{evidence}",
        "# Editable files\n\n"
        + "\n\n".join(f"## {path}\n\n```\n{text}\n```" for path, text in sorted(files.items())),
        f"This is proposal {index + 1} of {count}, written independently of the others.",
    ]
    return "\n\n".join(sections)


def failure_evidence(run: SuiteRun, *, max_trials: int) -> str:
    """The failing trials of ``run``, one scenario after another, as the generator reads them."""
    converted = convert(run.results, run.scenarios, run.personas)
    rows = [row for row in converted.rows if row.passed is False]
    by_scenario: dict[str, list[int]] = {}
    for position, row in enumerate(rows):
        by_scenario.setdefault(row.scenario_id, []).append(position)
    # Round-robin over scenarios so a few long-failing scenarios do not crowd the others out.
    order = [
        positions[depth]
        for depth in range(max((len(p) for p in by_scenario.values()), default=0))
        for positions in by_scenario.values()
        if depth < len(positions)
    ][:max_trials]
    trials = {trial.id: trial for trial in run.results.trials}
    blocks = []
    for position in order:
        row = rows[position]
        scenario = run.scenarios.get(row.scenario_id)
        causes = "\n".join(
            f"- {cause.cause}" + (f": {cause.rationale}" if cause.rationale else "")
            for cause in row.causes
        )
        transcript = render_transcript(run.results.events_of(trials[row.trial_id]))
        if len(transcript) > TRANSCRIPT_CHARS:
            transcript = "[...]\n" + transcript[-TRANSCRIPT_CHARS:]
        goal = scenario.goal if scenario is not None else "unknown"
        facts = json.dumps(scenario.facts, sort_keys=True) if scenario is not None else "{}"
        blocks.append(
            f"## Scenario {row.scenario_id} (trial {row.index})\n\n"
            f"Customer goal: {goal}\nCustomer facts: {facts}\n\n"
            f"Why it failed:\n{causes or '- (no cause recorded)'}\n\nTranscript:\n{transcript}"
        )
    return "\n\n".join(blocks)
