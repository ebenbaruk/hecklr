"""Stage 5, Synthesize: a regression scenario per cluster, deduplicated, queued for review.

The cluster's most central conversation becomes a scenario through the simulator's replay
extraction (``hecklr_sim.extract_scenario``): goal and grounded facts from the transcript, run
adaptively. It is re-tagged ``mined`` and points back at its cluster. A proposal whose title and
goal are too similar to a suite scenario, a queued proposal or an earlier proposal of the same
batch is dropped as a duplicate, and the reason is recorded.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import numpy as np

from hecklr_mine.embed import Embedder
from hecklr_schemas import (
    Conversation,
    FailureCluster,
    LLMProvider,
    ProposalId,
    Scenario,
    ScenarioProposal,
    ScenarioSource,
    new_id,
)
from hecklr_sim import extract_scenario

MINED_TAG: Final = "mined"
_SLUG = re.compile(r"[^a-z0-9]+")
_MAX_SLUG = 60


def mined_scenario_id(cluster: FailureCluster) -> str:
    """``mined-<label slug>-<cluster id suffix>``: readable, and unique per cluster."""
    slug = _SLUG.sub("-", cluster.label.lower()).strip("-")[:_MAX_SLUG].strip("-") or "cluster"
    return f"mined-{slug}-{cluster.id[-6:].lower()}"


async def synthesize_scenario(
    cluster: FailureCluster,
    exemplar: Conversation,
    llm: LLMProvider,
    *,
    model: str,
    world: str,
    persona: str,
    seed: int = 0,
) -> Scenario:
    """A ``mined`` scenario from the cluster's exemplar conversation.

    Raises:
        hecklr_llm.LLMOutputError: The extraction reply was unusable.
        hecklr_sim.ReplayExtractionError: The extraction found no goal.
    """
    extracted = await extract_scenario(
        exemplar, llm, model=model, world=world, persona=persona, seed=seed
    )
    tags = [MINED_TAG, *(tag for tag in extracted.tags if tag not in ("replay", MINED_TAG))]
    source = ScenarioSource(
        kind="mined", conversation_id=exemplar.id, cluster_id=cluster.id, note=cluster.label
    )
    # Validate again rather than model_copy: the result must load like an authored file.
    return Scenario.model_validate(
        {
            **extracted.model_dump(),
            "id": mined_scenario_id(cluster),
            "tags": tags,
            "source": source.model_dump(),
        }
    )


@dataclass(frozen=True, slots=True)
class Duplicate:
    """A synthesized scenario dropped because a similar one already exists."""

    cluster_id: str
    scenario_id: str
    duplicate_of: str
    similarity: float


def scenario_text(scenario: Scenario) -> str:
    """What dedup compares: the situation, not the wording of criteria."""
    return f"{scenario.title}. {scenario.goal}"


async def deduplicate(
    candidates: Sequence[tuple[FailureCluster, Scenario]],
    existing: Sequence[Scenario],
    embedder: Embedder,
    *,
    threshold: float,
) -> tuple[list[tuple[FailureCluster, Scenario]], list[Duplicate]]:
    """Split candidates into kept ones and duplicates, considering them in order.

    A candidate is kept when its cosine similarity to every existing scenario and every earlier
    kept candidate stays below ``threshold``.
    """
    if not candidates:
        return [], []
    texts = [scenario_text(s) for s in existing] + [scenario_text(s) for _, s in candidates]
    vectors = await embedder.embed(texts)
    reference = list(range(len(existing)))
    names = [s.id for s in existing]
    kept: list[tuple[FailureCluster, Scenario]] = []
    dropped: list[Duplicate] = []
    for offset, (cluster, scenario) in enumerate(candidates):
        row = len(existing) + offset
        if reference:
            similarities = vectors[reference] @ vectors[row]
            best = int(np.argmax(similarities))
            score = float(similarities[best])
            if score >= threshold:
                dropped.append(Duplicate(cluster.id, scenario.id, names[best], round(score, 4)))
                continue
        kept.append((cluster, scenario))
        reference.append(row)
        names.append(scenario.id)
    return kept, dropped


def propose(cluster: FailureCluster, scenario: Scenario) -> ScenarioProposal:
    """A fresh proposal awaiting review."""
    return ScenarioProposal(
        id=ProposalId(new_id()),
        cluster_id=cluster.id,
        scenario=scenario,
        created_at=datetime.now(UTC),
    )
