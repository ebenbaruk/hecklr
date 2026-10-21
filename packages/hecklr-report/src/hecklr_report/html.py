"""The single-file HTML report (US-03): run overview, comparisons and a per-trial viewer.

The file is self-contained: CSS, JavaScript and data are inline and nothing loads from the
network. Every piece of user or model text goes through Jinja2 autoescaping, and data for the
scripts is embedded with ``tojson`` (safe inside ``<script>``), so a transcript can never inject
markup. Recordings small enough are embedded as ``data:`` URIs; larger ones are linked by a path
relative to the report.
"""

import base64
import json
import os
import wave
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Final

import numpy as np
from jinja2 import Environment, PackageLoader, StrictUndefined
from pydantic import JsonValue

from hecklr_core import RunResults
from hecklr_report import fmt
from hecklr_report.model import Analysis, DegradationPoint, TrialRow
from hecklr_report.terminal import RUN_METRICS, VOICE_COLUMNS
from hecklr_schemas import (
    AudioSpanPayload,
    EmailPayload,
    ErrorPayload,
    Estimate,
    Event,
    EventPayload,
    Grade,
    HandoffPayload,
    InterruptPayload,
    SilencePayload,
    Slice,
    ToolCallPayload,
    ToolResultPayload,
    UtterancePayload,
    WorldSnapshot,
)

EMBED_AUDIO_BUDGET_BYTES: Final = 8 * 1024 * 1024
"""Recordings are embedded, failed trials first, until this many bytes are spent; the rest are
linked. A whole voice run embedded would make a report too large to open or upload."""

WAVEFORM_BUCKETS: Final = 800
"""Peak values drawn per recording: enough for a wide screen, small enough to embed per trial."""


@cache
def _environment() -> Environment:
    return Environment(
        loader=PackageLoader("hecklr_report", "templates"),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html(
    analysis: Analysis, results: RunResults, out_path: Path, *, run_dir: Path | None = None
) -> Path:
    """Write the HTML report for ``analysis`` to ``out_path`` and return the path.

    Args:
        analysis: The analysis of ``results`` (and its baseline).
        results: The candidate run's stored results, for the per-trial viewer.
        out_path: Where to write the report; parent directories are created.
        run_dir: The directory recording paths are relative to; defaults to the report's own
            directory, since reports are written into the run directory by default.

    Raises:
        ValueError: ``results`` are not the run ``analysis`` describes.
    """
    if analysis.candidate.id != results.run.id:
        raise ValueError(
            f"results of run {results.run.id} do not match the analysis of {analysis.candidate.id}"
        )
    root = run_dir if run_dir is not None else out_path.parent
    trials = _trial_views(analysis, results, root, out_path.parent)
    page = (
        _environment()
        .get_template("report.html.j2")
        .render(
            a=analysis,
            fmt=fmt,
            run_metrics=[m for m in RUN_METRICS if analysis.summary(m) is not None],
            voice_columns=VOICE_COLUMNS,
            voice_rows=_voice_rows(analysis),
            chart=_chart(analysis.voice.degradation) if analysis.voice is not None else None,
            trials=trials,
            audio=[audio.script_data() for trial in trials for audio in trial.audio],
        )
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    return out_path


# --- view models ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimelineItem:
    """One event in the trial timeline."""

    id: str
    t_ms: int
    actor: str
    kind: str
    text: str
    detail: str | None = None
    """Pretty JSON of arguments or results."""
    note: str | None = None
    """Timing or status, e.g. ``latency 812 ms``."""


@dataclass(frozen=True, slots=True)
class WorldChange:
    """One record that differs between ``world_before`` and ``world_after``."""

    collection: str
    key: str
    change: str
    before: str | None
    after: str | None


@dataclass(frozen=True, slots=True)
class Marker:
    """A span or instant on a recording, in ms from the recording's start."""

    start_ms: int
    end_ms: int
    actor: str
    kind: str


@dataclass(frozen=True, slots=True)
class AudioView:
    """One recording of a voice trial: player source, waveform peaks and turn markers."""

    index: int
    recording: str
    src: str | None
    """``data:`` URI or relative path; ``None`` when the file is missing."""
    duration_ms: int
    peaks: list[float] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)

    def script_data(self) -> dict[str, JsonValue]:
        """What the waveform script draws."""
        return {
            "duration_ms": self.duration_ms,
            "peaks": [round(p, 3) for p in self.peaks],
            "markers": [
                {"start": m.start_ms, "end": m.end_ms, "actor": m.actor, "kind": m.kind}
                for m in self.markers
            ],
        }


@dataclass(frozen=True, slots=True)
class TrialView:
    """Everything the per-trial viewer shows for one trial."""

    row: TrialRow
    title: str
    transcript: list[TimelineItem]
    timeline: list[TimelineItem]
    world: list[WorldChange]
    grades: list[Grade]
    audio: list[AudioView]


def _trial_views(
    analysis: Analysis, results: RunResults, root: Path, report_dir: Path
) -> list[TrialView]:
    rows = {row.trial_id: row for row in analysis.trials}
    titles = {s.scenario_id: s.title for s in analysis.scenarios}
    events: dict[str, list[Event]] = {}
    for event in results.events:
        events.setdefault(event.trial_id, []).append(event)
    grades: dict[str, list[Grade]] = {}
    for grade in results.grades:
        grades.setdefault(grade.trial_id, []).append(grade)
    # Failures first: they are what a reader opens the report for, and they get the audio budget.
    trials = sorted(
        results.trials,
        key=lambda t: (rows[t.id].passed is not False, t.scenario_id, t.index),
    )
    embedder = _Embedder(report_dir, EMBED_AUDIO_BUDGET_BYTES)
    audio_index = 0
    views = []
    for trial in trials:
        row = rows[trial.id]
        ordered = sorted(events.get(trial.id, []), key=lambda e: (e.t_ms, e.seq))
        timeline = [_timeline_item(e) for e in ordered]
        audio = _audio_views(ordered, trial.duration_ms, root, embedder, audio_index)
        audio_index += len(audio)
        views.append(
            TrialView(
                row=row,
                title=titles.get(trial.scenario_id, trial.scenario_id),
                transcript=[i for i in timeline if i.kind in ("utterance", "email")],
                timeline=timeline,
                world=world_diff(trial.world_before, trial.world_after),
                grades=sorted(
                    grades.get(trial.id, []),
                    key=lambda g: (g.criterion_index is None, g.criterion_index or 0, g.grader),
                ),
                audio=audio,
            )
        )
    return views


def _pretty(value: JsonValue) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def _timeline_item(event: Event) -> TimelineItem:
    """A readable line for any event kind."""
    text, detail, note = _describe(event.payload)
    return TimelineItem(
        id=event.id,
        t_ms=event.t_ms,
        actor=event.actor,
        kind=event.kind,
        text=text,
        detail=detail,
        note=note,
    )


def _describe(p: EventPayload) -> tuple[str, str | None, str | None]:
    """(text, pretty JSON detail, timing or status note) of one payload."""
    match p:
        case UtterancePayload():
            latency = f"latency {p.latency_ms} ms" if p.latency_ms is not None else None
            return p.text, None, _join([latency, "interrupted" if p.interrupted else None])
        case ToolCallPayload():
            return f"call {p.tool}", _pretty(p.args), p.source
        case ToolResultPayload():
            status = f"error: {p.error}" if p.error is not None else "ok"
            fault = f"fault {p.fault.value}" if p.fault else None
            detail = _pretty(p.result) if p.error is None else None
            return f"{p.tool} {status}", detail, _join([f"{p.latency_ms} ms", fault])
        case AudioSpanPayload():
            return f"{p.content} {p.start_ms}\u2013{p.end_ms} ms", None, None
        case InterruptPayload():
            reaction = (
                "never stopped" if p.reaction_ms is None else f"stopped in {p.reaction_ms} ms"
            )
            return f"{p.style.replace('_', '-')} over {p.target}", None, reaction
        case SilencePayload():
            whose = p.attributed_to or "nobody"
            text = f"{p.reason.replace('_', ' ')} silence ({whose})"
            return text, None, f"{p.end_ms - p.start_ms} ms"
        case EmailPayload():
            m = p.message
            return f"Subject: {m.subject}\n\n{m.body}", None, m.message_id
        case HandoffPayload():
            return f"handoff to {p.target or 'a human'}", None, p.reason
        case ErrorPayload():
            return f"{p.source} error: {p.message}", None, None


def _join(parts: Sequence[str | None]) -> str | None:
    kept = [p for p in parts if p]
    return " · ".join(kept) if kept else None


def world_diff(before: WorldSnapshot, after: WorldSnapshot) -> list[WorldChange]:
    """Records added, removed or changed between two snapshots, by collection and key."""
    changes = []
    for collection in sorted(before.collections.keys() | after.collections.keys()):
        old = before.collections.get(collection, {})
        new = after.collections.get(collection, {})
        for key in sorted(old.keys() | new.keys()):
            if key not in new:
                changes.append(WorldChange(collection, key, "removed", _pretty(old[key]), None))
            elif key not in old:
                changes.append(WorldChange(collection, key, "added", None, _pretty(new[key])))
            elif old[key] != new[key]:
                changed = sorted(
                    f
                    for f in old[key].keys() | new[key].keys()
                    if old[key].get(f) != new[key].get(f)
                )
                changes.append(
                    WorldChange(
                        collection,
                        key,
                        "changed: " + ", ".join(changed),
                        _pretty({f: old[key].get(f) for f in changed}),
                        _pretty({f: new[key].get(f) for f in changed}),
                    )
                )
    return changes


# --- audio ------------------------------------------------------------------------------------


def _audio_views(
    events: Sequence[Event], duration_ms: int, root: Path, embedder: "_Embedder", first_index: int
) -> list[AudioView]:
    """One view per recording referenced by the trial's audio spans."""
    spans = [(e, e.payload) for e in events if isinstance(e.payload, AudioSpanPayload)]
    recordings = sorted({p.recording for _, p in spans if p.recording})
    views = []
    for offset, recording in enumerate(recordings):
        # Sample-accurate placement when the voice engine recorded it; else the recording is
        # assumed to start with the trial (both clocks share the trial origin, ADR 0006).
        origin = next(
            (
                p.start_ms - round(p.start_sample * 1000 / p.sample_rate)
                for _, p in spans
                if p.recording == recording and p.start_sample is not None and p.sample_rate
            ),
            0,
        )
        path = root / recording
        peaks, length_ms = _waveform(path)
        markers = [
            Marker(p.start_ms - origin, p.end_ms - origin, e.actor, p.content)
            for e, p in spans
            if p.recording in (recording, None)
        ]
        markers += [
            Marker(e.t_ms - origin, e.t_ms - origin, e.actor, e.payload.style)
            for e in events
            if isinstance(e.payload, InterruptPayload)
        ]
        views.append(
            AudioView(
                index=first_index + offset,
                recording=recording,
                src=embedder.src(path),
                duration_ms=length_ms or max([duration_ms, *(m.end_ms for m in markers)]),
                peaks=peaks,
                markers=markers,
            )
        )
    return views


@dataclass(slots=True)
class _Embedder:
    """Embeds recordings as ``data:`` URIs while the budget lasts, then links them."""

    report_dir: Path
    budget: int

    def src(self, path: Path) -> str | None:
        """The player source for ``path``; ``None`` when the file is missing."""
        if not path.is_file():
            return None
        size = path.stat().st_size
        if size > self.budget:
            return Path(os.path.relpath(path, self.report_dir)).as_posix()
        self.budget -= size
        return "data:audio/wav;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _waveform(path: Path) -> tuple[list[float], int]:
    """Peak amplitude per bucket (0 to 1) and the length in ms of a 16-bit PCM WAV file.

    Returns no peaks for a missing file or another sample format; the player still works and the
    turn markers are drawn on an empty track.
    """
    if not path.is_file():
        return [], 0
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            return [], round(wav.getnframes() * 1000 / wav.getframerate())
        frames = wav.readframes(wav.getnframes())
        channels, rate = wav.getnchannels(), wav.getframerate()
    samples = np.frombuffer(frames, dtype="<i2").reshape(-1, channels).astype(np.float64)
    mono = np.abs(samples).max(axis=1) / 32768.0
    length_ms = round(mono.size * 1000 / rate)
    if mono.size == 0:
        return [], length_ms
    buckets = np.array_split(mono, min(WAVEFORM_BUCKETS, mono.size))
    return [float(bucket.max()) for bucket in buckets], length_ms


# --- overview helpers -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VoiceRow:
    """One slice of the voice metrics table."""

    label: str
    cells: list[Estimate | None]


def _voice_rows(analysis: Analysis) -> list[VoiceRow]:
    if analysis.voice is None:
        return []
    slices: list[Slice] = []
    for summary in analysis.voice.metrics:
        if summary.slice not in slices:
            slices.append(summary.slice)
    found = {(m.metric, tuple(sorted(m.slice.items()))): m.estimate for m in analysis.voice.metrics}
    return [
        VoiceRow(
            label=fmt.slice_label(s) if s else "all voice calls",
            cells=[found.get((metric, tuple(sorted(s.items())))) for metric, _ in VOICE_COLUMNS],
        )
        for s in slices
    ]


@dataclass(frozen=True, slots=True)
class ChartPoint:
    """One bucket of the degradation chart, in SVG coordinates."""

    label: str
    x: float
    y: float
    y_low: float
    y_high: float
    text: str
    base_y: float | None


CHART_WIDTH, CHART_HEIGHT, CHART_PAD = 560, 220, 36


def _chart(points: Sequence[DegradationPoint]) -> list[ChartPoint] | None:
    """Degradation curve (US-07): pass rate with its interval per SNR bucket."""
    if not points:
        return None
    step = (CHART_WIDTH - 2 * CHART_PAD) / max(1, len(points) - 1)

    def y(rate: float) -> float:
        return CHART_PAD + (1 - rate) * (CHART_HEIGHT - 2 * CHART_PAD)

    return [
        ChartPoint(
            label=p.label,
            x=CHART_PAD + i * step if len(points) > 1 else CHART_WIDTH / 2,
            y=y(p.pass_rate.value),
            y_low=y(p.pass_rate.ci_low),
            y_high=y(p.pass_rate.ci_high),
            text=fmt.estimate(p.pass_rate, "percent"),
            base_y=y(p.baseline_pass_rate.value) if p.baseline_pass_rate else None,
        )
        for i, p in enumerate(points)
    ]
