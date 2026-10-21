"""The HTML report: one self-contained file, escaped content, and the US-03 trial viewer."""

from __future__ import annotations

import io
import re
import struct
import wave
from html.parser import HTMLParser
from typing import TYPE_CHECKING

import numpy as np
import pytest

from hecklr_report import analyze, render_html
from hecklr_report import html as html_module
from hecklr_report.html import world_diff
from hecklr_schemas import (
    Channel,
    EmailMessage,
    EmailPayload,
    ErrorPayload,
    FaultKind,
    HandoffPayload,
    Scenario,
    ToolResultPayload,
    WorldSnapshot,
)

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import Kit
    from hecklr_core import RunResults
    from hecklr_report import Analysis

XSS = "<script>alert('pwned')</script></script><img src=x onerror=alert(1)>"


class Tags(HTMLParser):
    """Collects every start tag with its attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def parse(page: str) -> list[tuple[str, dict[str, str | None]]]:
    parser = Tags()
    parser.feed(page)
    return parser.tags


def render(results: RunResults, kit: type[Kit], scenarios: dict[str, Scenario], tmp: Path) -> str:
    """The HTML report of a single run, as text."""
    analysis = analyze(results, None, kit.stats, scenarios)
    return render_html(analysis, results, tmp / "r.html").read_text(encoding="utf-8")


def write_wav(path: Path, seconds: float = 2.0, rate: int = 16_000) -> None:
    t = np.arange(int(seconds * rate)) / rate
    samples = (0.4 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())


def test_us03_report_is_one_self_contained_file(
    regression: tuple[Analysis, RunResults], tmp_path: Path
) -> None:
    analysis, results = regression

    path = render_html(analysis, results, tmp_path / "nested" / "report.html")

    page = path.read_text(encoding="utf-8")
    assert path == tmp_path / "nested" / "report.html"
    assert list(path.parent.iterdir()) == [path]
    for tag, attrs in parse(page):
        assert tag != "link", "stylesheets must be inline"
        for name in ("src", "href"):
            value = attrs.get(name) or ""
            assert not re.match(r"^(https?:)?//", value), f"<{tag} {name}={value}> loads remotely"
    assert "@import" not in page
    assert "url(http" not in page
    assert page.count("<script") == 2


def test_us03_overview_comparisons_flips_and_trials_are_shown(
    regression: tuple[Analysis, RunResults], tmp_path: Path
) -> None:
    analysis, results = regression

    page = render_html(analysis, results, tmp_path / "report.html").read_text(encoding="utf-8")

    assert '<span class="badge v-regression">regression</span>' in page
    assert "pass_rate" in page
    assert "95% CI" in page
    assert "Flipped scenarios, grouped by cause" in page
    assert "The agent never confirmed the new slot." in page
    assert page.count('<details class="trial"') == len(results.trials)
    first_trial = page.index('<details class="trial"')
    assert page.index("FAIL", first_trial) < page.index("PASS", first_trial)


def test_us03_trial_viewer_has_transcript_tools_world_diff_and_evidence(
    kit: type[Kit], scenarios: dict[str, Scenario], tmp_path: Path
) -> None:
    builder = kit.RunBuilder()
    builder.trial("s00", passed=False)
    results = builder.build()

    page = render(results, kit, scenarios, tmp_path)

    assert '<div class="bubble user">' in page
    assert "Can you move my visit?" in page
    assert "call reschedule_appointment" in page
    assert "latency 400 ms" in page
    assert "appointments/C-1042" in page
    assert "changed: slot" in page
    assert "notes/N-1" in page
    evidence = results.grades[1].evidence[0].event_id
    assert f'href="#ev-{evidence}"' in page
    assert f'id="ev-{evidence}"' in page


def test_us03_user_content_is_escaped(
    kit: type[Kit], scenarios: dict[str, Scenario], tmp_path: Path
) -> None:
    builder = kit.RunBuilder()
    builder.trial(
        "s00",
        passed=False,
        user_text=XSS,
        rationale=XSS,
        channel=Channel.VOICE,
        voice=kit.VoiceShape(recording=f"audio/{XSS}.wav"),
    )
    results = builder.build()

    page = render(results, kit, scenarios, tmp_path)

    assert "<script>alert" not in page
    assert "<img src=x" not in page
    assert "&lt;script&gt;alert(&#39;pwned&#39;)&lt;/script&gt;" in page
    assert page.count("<script") == 2
    assert not any(tag == "img" for tag, _ in parse(page))


def test_us03_voice_trial_embeds_audio_with_waveform_and_turn_markers(
    kit: type[Kit], scenarios: dict[str, Scenario], tmp_path: Path
) -> None:
    write_wav(tmp_path / "audio" / "call.wav")
    builder = kit.RunBuilder()
    builder.trial("s00", channel=Channel.VOICE, voice=kit.VoiceShape(recording="audio/call.wav"))
    results = builder.build()

    page = render(results, kit, scenarios, tmp_path)

    assert 'src="data:audio/wav;base64,UklGR' in page
    assert '<canvas class="wave" data-audio="0"' in page
    data = re.search(r'id="hecklr-audio">(.*?)</script>', page, re.S)
    assert data is not None
    assert '"duration_ms": 2000' in data.group(1)
    assert '"actor": "agent"' in data.group(1)
    assert '"kind": "barge_in"' in data.group(1)
    assert "Voice" in page


def test_large_recordings_are_linked_relative_to_the_report(
    kit: type[Kit],
    scenarios: dict[str, Scenario],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(html_module, "EMBED_AUDIO_BUDGET_BYTES", 10)
    run_dir = tmp_path / "run"
    write_wav(run_dir / "audio" / "call.wav")
    builder = kit.RunBuilder()
    builder.trial("s00", channel=Channel.VOICE, voice=kit.VoiceShape(recording="audio/call.wav"))
    results = builder.build()
    analysis = analyze(results, None, kit.stats, scenarios)

    page = render_html(analysis, results, tmp_path / "out" / "r.html", run_dir=run_dir)

    assert 'src="../run/audio/call.wav"' in page.read_text(encoding="utf-8")


def test_failed_trials_get_the_audio_budget_first(
    kit: type[Kit],
    scenarios: dict[str, Scenario],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_wav(tmp_path / "audio" / "ok.wav")
    write_wav(tmp_path / "audio" / "failed.wav")
    monkeypatch.setattr(
        html_module, "EMBED_AUDIO_BUDGET_BYTES", (tmp_path / "audio" / "ok.wav").stat().st_size
    )
    builder = kit.RunBuilder()
    builder.trial("s00", channel=Channel.VOICE, voice=kit.VoiceShape(recording="audio/ok.wav"))
    builder.trial(
        "s01",
        passed=False,
        channel=Channel.VOICE,
        voice=kit.VoiceShape(recording="audio/failed.wav"),
    )

    page = render(builder.build(), kit, scenarios, tmp_path)

    assert page.count('src="data:audio/wav;base64,') == 1
    assert 'src="audio/ok.wav"' in page
    assert page.index("audio/failed.wav") < page.index("data:audio/wav") < page.index("ok.wav")


def test_missing_recordings_still_draw_turn_markers(
    kit: type[Kit], scenarios: dict[str, Scenario], tmp_path: Path
) -> None:
    builder = kit.RunBuilder()
    builder.trial("s00", channel=Channel.VOICE, voice=kit.VoiceShape(recording="audio/gone.wav"))
    results = builder.build()

    page = render(results, kit, scenarios, tmp_path)

    assert "recording not found next to the report; markers only" in page
    assert "<audio" not in page
    assert '<canvas class="wave" data-audio="0"' in page


def test_non_pcm16_recordings_play_without_a_waveform(
    kit: type[Kit], scenarios: dict[str, Scenario], tmp_path: Path
) -> None:
    path = tmp_path / "audio" / "call.wav"
    path.parent.mkdir()
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(1)
        wav.setframerate(8_000)
        wav.writeframes(bytes(8_000))
    builder = kit.RunBuilder()
    builder.trial("s00", channel=Channel.VOICE, voice=kit.VoiceShape(recording="audio/call.wav"))
    results = builder.build()

    page = render(results, kit, scenarios, tmp_path)

    assert '"peaks": []' in page


def write_stereo_wav(path: Path, frames: int = 4_000) -> None:
    samples = np.full(frames * 2, 12_000, dtype="<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(8_000)
        wav.writeframes(samples.tobytes())


def voice_page(kit: type[Kit], scenarios: dict[str, Scenario], tmp: Path, recording: str) -> str:
    builder = kit.RunBuilder()
    builder.trial("s00", channel=Channel.VOICE, voice=kit.VoiceShape(recording=recording))
    return render(builder.build(), kit, scenarios, tmp)


def test_truncated_recording_is_drawn_up_to_its_last_whole_frame(
    kit: type[Kit], scenarios: dict[str, Scenario], tmp_path: Path
) -> None:
    path = tmp_path / "audio" / "cut.wav"
    write_stereo_wav(path)
    path.write_bytes(path.read_bytes()[:-3])

    page = voice_page(kit, scenarios, tmp_path, "audio/cut.wav")

    assert "data:audio/wav;base64," in page
    assert '"peaks": []' not in page
    assert "unreadable" not in page


def wav_bytes(rate: int, frames: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8_000)
        wav.writeframes(frames)
    data = bytearray(buffer.getvalue())
    data[24:28] = struct.pack("<I", rate)  # the fmt chunk's sample rate field
    return bytes(data)


def test_header_only_recording_plays_without_a_waveform(
    kit: type[Kit], scenarios: dict[str, Scenario], tmp_path: Path
) -> None:
    path = tmp_path / "audio" / "silent.wav"
    path.parent.mkdir()
    path.write_bytes(wav_bytes(8_000, b""))

    page = voice_page(kit, scenarios, tmp_path, "audio/silent.wav")

    assert "data:audio/wav;base64," in page
    assert '"peaks": []' in page
    assert "unreadable" not in page


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"", "recording unreadable: "),
        (b"OggS" + bytes(64), "recording unreadable: "),
        (wav_bytes(0, bytes(100)), "recording unreadable: frame rate 0 Hz"),
    ],
    ids=["empty", "not-riff", "zero-rate"],
)
def test_unreadable_recording_keeps_markers_and_says_why(
    kit: type[Kit], scenarios: dict[str, Scenario], tmp_path: Path, content: bytes, reason: str
) -> None:
    path = tmp_path / "audio" / "bad.wav"
    path.parent.mkdir()
    path.write_bytes(content)

    page = voice_page(kit, scenarios, tmp_path, "audio/bad.wav")

    assert reason in page
    assert "; markers only" in page
    assert '"peaks": []' in page
    assert '<canvas class="wave" data-audio="0"' in page


def test_recording_paths_outside_the_run_directory_are_not_embedded(
    kit: type[Kit], scenarios: dict[str, Scenario], tmp_path: Path
) -> None:
    secret = tmp_path / "secret.wav"
    write_wav(secret)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    for recording in ("../secret.wav", str(secret)):
        builder = kit.RunBuilder()
        builder.trial("s00", channel=Channel.VOICE, voice=kit.VoiceShape(recording=recording))
        results = builder.build()
        analysis = analyze(results, None, kit.stats, scenarios)
        page = render_html(analysis, results, run_dir / "r.html").read_text(encoding="utf-8")

        assert "recording path is outside the run directory; markers only" in page
        assert "<audio" not in page


def test_us06_us07_voice_section_has_degradation_curve_and_metric_tables(
    kit: type[Kit], scenarios: dict[str, Scenario], tmp_path: Path
) -> None:
    personas = {
        "clean": kit.persona("clean", accent="en-GB"),
        "noisy": kit.persona("noisy", snr_db=10, accent="fr-FR"),
    }
    builder = kit.RunBuilder()
    for persona in personas:
        builder.suite(
            {"s00": 0.8, "s01": 0.5},
            3,
            rng_seed=2,
            channel=Channel.VOICE,
            persona_id=persona,
            voice=kit.VoiceShape(),
        )
    results = builder.build()
    analysis = analyze(results, None, kit.stats, scenarios, personas=personas)

    page = render_html(analysis, results, tmp_path / "r.html").read_text(encoding="utf-8")

    assert "Degradation curve: success rate by SNR (US-07)" in page
    assert '<svg viewBox="0 0 560 240"' in page
    assert "Success rate by accent" in page
    assert "Turn-taking by noise level and accent (US-06, 95% CI)" in page
    assert "snr_db=10" in page
    assert "accent=fr-FR" in page
    assert "barge-in p95" in page


def test_timeline_describes_email_handoff_error_and_tool_failures(
    kit: type[Kit], scenarios: dict[str, Scenario], tmp_path: Path
) -> None:
    builder = kit.RunBuilder()
    trial = builder.trial("s00", passed=False)
    log = kit.TrialLog(trial.id)
    log.events = [e for e in builder.events if e.trial_id == trial.id]
    message = EmailMessage(
        message_id="<m1@acme.test>",
        subject="Your visit",
        body="Moved to Tuesday.",
        from_addr="support@acme.test",
        to_addrs=["c@acme.test"],
    )
    log.add("agent", EmailPayload(message=message), 9_000)
    log.add("agent", HandoffPayload(reason="gas smell", target="dispatcher"), 9_100)
    log.add("system", ErrorPayload(source="world", message="tool exploded"), 9_200)
    log.add(
        "tool",
        ToolResultPayload(
            call_id="c2", tool="lookup", error="timeout", latency_ms=5_000, fault=FaultKind.TIMEOUT
        ),
        9_300,
    )
    builder.events = [e for e in builder.events if e.trial_id != trial.id] + log.events
    results = builder.build()

    page = render(results, kit, scenarios, tmp_path)

    assert "Subject: Your visit" in page
    assert "handoff to dispatcher" in page
    assert "world error: tool exploded" in page
    assert "lookup error: timeout" in page
    assert "fault timeout" in page


def test_results_of_another_run_are_refused(
    regression: tuple[Analysis, RunResults], kit: type[Kit], tmp_path: Path
) -> None:
    other = kit.RunBuilder().build()

    with pytest.raises(ValueError, match="do not match the analysis"):
        render_html(regression[0], other, tmp_path / "r.html")


def test_world_diff_lists_added_removed_and_changed_records() -> None:
    before = WorldSnapshot(
        world_id="w",
        taken_at_ms=0,
        collections={"a": {"k1": {"x": 1, "y": 2}, "gone": {"x": 0}}},
    )
    after = WorldSnapshot(
        world_id="w",
        taken_at_ms=5,
        collections={"a": {"k1": {"x": 1, "y": 3}, "new": {"x": 9}}, "b": {"k": {}}},
    )

    changes = world_diff(before, after)

    assert [(c.collection, c.key, c.change) for c in changes] == [
        ("a", "gone", "removed"),
        ("a", "k1", "changed: y"),
        ("a", "new", "added"),
        ("b", "k", "added"),
    ]
    assert changes[1].before == '{\n  "y": 2\n}'
    assert changes[1].after == '{\n  "y": 3\n}'
