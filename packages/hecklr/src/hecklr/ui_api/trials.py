"""The trial viewer's data (US-03): timeline, grades, world diff and recordings with markers."""

from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote

from hecklr.ui_api.models import Marker, Recording, TrialDetail, WorldChange
from hecklr_core import RunResults
from hecklr_report import Analysis
from hecklr_report.html import world_diff
from hecklr_schemas import AudioSpanPayload, Event, HumanLabel, InterruptPayload, Trial

RECORDING_SUFFIXES = (".wav",)


class UnknownTrialError(LookupError):
    """The run holds no trial with this id."""


def trial_detail(
    results: RunResults,
    analysis: Analysis | None,
    trial_id: str,
    run_dir: Path,
    labels: Sequence[HumanLabel],
) -> TrialDetail:
    """Everything stored about one trial of ``results``.

    Raises:
        UnknownTrialError: no such trial in the run.
    """
    trial = next((t for t in results.trials if t.id == trial_id), None)
    if trial is None:
        raise UnknownTrialError(f"run {results.run.id} has no trial {trial_id!r}")
    events = sorted(results.events_of(trial), key=lambda e: (e.t_ms, e.seq))
    row = None
    title = trial.scenario_id
    if analysis is not None:
        row = next((r for r in analysis.trials if r.trial_id == trial.id), None)
        title = next(
            (s.title for s in analysis.scenarios if s.scenario_id == trial.scenario_id), title
        )
    grades = sorted(
        results.grades_of(trial),
        key=lambda g: (g.criterion_index is None, g.criterion_index or 0, g.grader),
    )
    return TrialDetail(
        trial=trial,
        row=row,
        title=title,
        events=events,
        grades=grades,
        world_changes=_world_changes(trial),
        recordings=recordings(events, results.run.id, run_dir),
        labels=[label for label in labels if label.trial_id == trial.id],
    )


def _world_changes(trial: Trial) -> list[WorldChange]:
    return [
        WorldChange(
            collection=c.collection, key=c.key, change=c.change, before=c.before, after=c.after
        )
        for c in world_diff(trial.world_before, trial.world_after)
    ]


def recordings(events: Sequence[Event], run_id: str, run_dir: Path) -> list[Recording]:
    """One entry per recording the trial's audio spans reference, with its turn markers."""
    spans = [(e, e.payload) for e in events if isinstance(e.payload, AudioSpanPayload)]
    interrupts = [(e, e.payload) for e in events if isinstance(e.payload, InterruptPayload)]
    views = []
    for path in sorted({p.recording for _, p in spans if p.recording}):
        # Sample-accurate placement when the voice engine recorded it; else the recording is
        # assumed to start with the trial (both clocks share the trial origin, ADR 0006).
        origin = next(
            (
                p.start_ms - round(p.start_sample * 1000 / p.sample_rate)
                for _, p in spans
                if p.recording == path and p.start_sample is not None and p.sample_rate
            ),
            0,
        )
        markers = [
            Marker(
                start_ms=p.start_ms - origin,
                end_ms=p.end_ms - origin,
                actor=e.actor,
                kind=p.content,
            )
            for e, p in spans
            if p.recording in (path, None)
        ]
        markers += [
            Marker(start_ms=e.t_ms - origin, end_ms=e.t_ms - origin, actor=e.actor, kind=p.style)
            for e, p in interrupts
        ]
        file, note = locate_recording(run_dir, path)
        views.append(
            Recording(
                path=path,
                url=f"/api/runs/{run_id}/recordings/{quote(path)}" if file is not None else None,
                origin_ms=origin,
                markers=markers,
                note=note,
            )
        )
    return views


def locate_recording(run_dir: Path, recording: str) -> tuple[Path | None, str | None]:
    """The recording's file in the run directory, or ``None`` and why it cannot be served.

    Paths come from stored events; one escaping the run directory (absolute or ``..``) or naming
    something other than a WAV file would let a request read arbitrary local files.
    """
    base = run_dir.resolve()
    path = (base / recording).resolve()
    if not path.is_relative_to(base) or path.suffix.lower() not in RECORDING_SUFFIXES:
        return None, "recording path is outside the run directory or not a WAV file"
    if not path.is_file():
        return None, "recording not found in the run directory"
    return path, None
