"""What to fix: one failure cluster and the scenarios that reproduce it.

Two sources:

- the miner's store (ADR 0013a), read through ``hecklr_mine``: a ``FailureCluster`` of a mining
  run and the scenarios proposed from it (approved or still proposed; rejected ones are not the
  cluster's behaviour as a person judged it);
- a failing run: the scenarios whose trials failed (``--from-run``), or, against a baseline run,
  the scenarios whose pass rate dropped (the "regression -> fix" flow), optionally narrowed by tag
  or scenario id. The cluster is built from those trials (``source: run``).
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from hecklr_core import RunResults
from hecklr_fix.errors import FixError
from hecklr_report.model import TrialRow
from hecklr_report.outcomes import convert
from hecklr_schemas import (
    Channel,
    ClusterId,
    FailureCluster,
    Persona,
    ProposalStatus,
    Scenario,
    TrialId,
    new_id,
)


@dataclass(frozen=True, slots=True)
class FixTarget:
    """A failure cluster and the scenarios the targeted pass runs."""

    cluster: FailureCluster
    scenarios: tuple[Scenario, ...]
    channels: tuple[Channel, ...] | None = None
    """Channels to run the scenarios on; each scenario's own when ``None``."""
    personas: str | None = None
    """Persona glob to cross the scenarios with (the source suite's); scenario personas if
    ``None``."""


def load_mined_target(mine_root: Path, cluster_id: str) -> FixTarget:
    """The cluster ``cluster_id`` from ``<output_dir>/mine`` and its proposed scenarios.

    Raises:
        FixError: no such cluster, none of its proposals is still proposed or approved, or the
            miner's store cannot be read.
    """
    # Imported here: hecklr_mine loads scikit-learn, which every other command would pay for at
    # start-up (as in ``hecklr.commands.mine``).
    from hecklr_mine import MineError, load_clusters, load_queue

    try:
        cluster = next((c for c in load_clusters(mine_root) if c.id == cluster_id), None)
        if cluster is None:
            raise FixError(f"no cluster {cluster_id} under {mine_root}")
        scenarios = [
            proposal.scenario
            for proposal in load_queue(mine_root)  # the newest record of each proposal
            if proposal.cluster_id == cluster_id and proposal.status is not ProposalStatus.REJECTED
        ]
    except MineError as exc:
        raise FixError(str(exc)) from exc
    if not scenarios:
        raise FixError(
            f"cluster {cluster_id} has no proposed or approved scenario to run; "
            "re-run hecklr mine with a model so it synthesizes one"
        )
    return FixTarget(cluster=cluster, scenarios=tuple(scenarios))


def target_from_run(
    candidate: RunResults,
    scenarios: Mapping[str, Scenario],
    personas: Mapping[str, Persona],
    *,
    baseline: RunResults | None = None,
    tags: Collection[str] = (),
    scenario_ids: Collection[str] = (),
    channels: Sequence[Channel] | None = None,
    personas_glob: str | None = None,
) -> FixTarget:
    """The failing scenarios of ``candidate`` as a fix target.

    Without ``baseline``, a scenario fails when any of its analyzed trials failed; with it, when
    its pass rate is lower than in ``baseline`` (scenarios missing from the baseline are skipped).

    Raises:
        FixError: no scenario matches.
    """
    rows = convert(candidate, scenarios, personas).rows
    failed = _rates(rows)
    if baseline is not None:
        before = _rates(convert(baseline, scenarios, personas).rows)
        failed = {s: rate for s, rate in failed.items() if s in before and rate < before[s]}
    else:
        failed = {s: rate for s, rate in failed.items() if rate < 1.0}
    chosen = [
        scenario_id
        for scenario_id in sorted(failed)
        if scenario_id in scenarios
        and (not tags or set(tags) & set(scenarios[scenario_id].tags))
        and (not scenario_ids or scenario_id in scenario_ids)
    ]
    if not chosen:
        what = "dropped against the baseline" if baseline is not None else "failed"
        raise FixError(
            f"no scenario of run {candidate.run.id} {what}"
            f"{_filter_text(tags, scenario_ids)}: nothing to fix"
        )
    failing = [row for row in rows if row.scenario_id in chosen and row.passed is False]
    exemplars = list({row.scenario_id: row.trial_id for row in reversed(failing)}.values())
    label = f"fails {', '.join(chosen)}" if len(chosen) <= 3 else f"fails {len(chosen)} scenarios"
    cluster = FailureCluster(
        id=ClusterId(new_id()),
        label=label,
        description=(
            f"Scenarios of run {candidate.run.id} that "
            + (f"dropped against run {baseline.run.id}" if baseline is not None else "failed")
            + _filter_text(tags, scenario_ids)
        ),
        source="run",
        trial_ids=[TrialId(row.trial_id) for row in failing],
        exemplar_trial_ids=[TrialId(trial_id) for trial_id in sorted(exemplars)],
        created_at=datetime.now(UTC),
    )
    return FixTarget(
        cluster=cluster,
        scenarios=tuple(scenarios[s] for s in chosen),
        channels=tuple(channels) if channels is not None else None,
        personas=personas_glob,
    )


def _rates(rows: Sequence[TrialRow]) -> dict[str, float]:
    tallies: dict[str, list[int]] = {}
    for row in rows:
        if row.passed is not None:
            tally = tallies.setdefault(row.scenario_id, [0, 0])
            tally[0] += 1
            tally[1] += int(row.passed)
    return {scenario: passes / trials for scenario, (trials, passes) in tallies.items()}


def _filter_text(tags: Collection[str], scenario_ids: Collection[str]) -> str:
    parts = []
    if tags:
        parts.append(f"tagged {', '.join(sorted(tags))}")
    if scenario_ids:
        parts.append(f"among {', '.join(sorted(scenario_ids))}")
    return f" ({'; '.join(parts)})" if parts else ""
