"""``voice.yaml``, its fault overlays F7-F10, transcript normalization and the command line."""

import difflib
from pathlib import Path

import pytest
import yaml
from acme_voice.__main__ import main, options_from, parse_args, speech_stack
from acme_voice.config import (
    FAULTS_ROOT,
    PROJECT_ROOT,
    VOICE_FILE,
    PipelineConfig,
    VoiceConfigError,
    load_pipeline_config,
    voice_fault_overlay,
)
from acme_voice.normalize import normalize_transcript

from hecklr_voice import EnergyVad, FakeASR, SileroVad, SyntheticTTS

VOICE_FAULTS = {
    "F7": ("tts_pre_delay_ms", 700),
    "F8": ("barge_in", False),
    "F9": ("end_of_speech_ms", 150),
    "F10": ("asr_model", "faster-whisper/tiny"),
}


def test_the_clean_pipeline_is_the_reference_agent() -> None:
    clean = load_pipeline_config()
    assert clean == PipelineConfig()  # the file and the code defaults agree
    assert clean.variant == "clean"
    assert (clean.asr_model, clean.vad, clean.tts_provider) == (
        "faster-whisper/base.en",
        "silero",
        "kokoro",
    )


@pytest.mark.parametrize("fault", sorted(VOICE_FAULTS))
def test_sc8_each_voice_fault_changes_exactly_one_line_of_voice_yaml(fault: str) -> None:
    clean = (PROJECT_ROOT / VOICE_FILE).read_text().splitlines()
    faulty = (voice_fault_overlay(fault) / VOICE_FILE).read_text().splitlines()
    changed = [
        line
        for line in difflib.unified_diff(clean, faulty, lineterm="", n=0)
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    assert len(changed) == 2, changed
    assert sorted(path.name for path in (FAULTS_ROOT / fault).iterdir()) == [
        "README.md",
        "voice.yaml",
    ]


@pytest.mark.parametrize("fault", sorted(VOICE_FAULTS))
def test_sc8_each_voice_fault_flips_exactly_its_setting(fault: str) -> None:
    setting, value = VOICE_FAULTS[fault]
    clean = load_pipeline_config()
    faulty = load_pipeline_config(overlay=voice_fault_overlay(fault))
    assert getattr(faulty, setting) == value != getattr(clean, setting)
    differing = {
        name
        for name in PipelineConfig.__dataclass_fields__
        if getattr(faulty, name) != getattr(clean, name)
    }
    assert differing == {setting, "variant"}
    assert faulty.variant == fault


def test_fault_readmes_name_the_catching_metric() -> None:
    metrics = {
        "F7": "response_latency",
        "F8": "barge_in_reaction",
        "F9": "false_interruptions",
        "F10": "10 dB",
    }
    for fault, metric in metrics.items():
        assert metric in (FAULTS_ROOT / fault / "README.md").read_text()


@pytest.mark.parametrize(
    ("fault", "message"),
    [("bad", r"F7\.\.F10"), ("F1", "not a voice fault variant"), ("F99", "not a voice fault")],
)
def test_unknown_or_chat_faults_are_refused(fault: str, message: str) -> None:
    with pytest.raises(VoiceConfigError, match=message):
        voice_fault_overlay(fault)


def test_an_overlay_may_not_carry_other_files(tmp_path: Path) -> None:
    (tmp_path / "voice.yaml").write_text("barge_in: false\n")
    (tmp_path / "pipeline.py").write_text("print('changed code')\n")
    with pytest.raises(VoiceConfigError, match=r"may only replace voice\.yaml"):
        load_pipeline_config(overlay=tmp_path)
    with pytest.raises(VoiceConfigError, match="not a directory"):
        load_pipeline_config(overlay=tmp_path / "missing")


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("- a list\n", "a mapping"),
        ("speed: 2\n", "unknown settings"),
        ("end_of_speech_ms: '600'\n", "whole number"),
        ("end_of_speech_ms: 10\n", "at least one 20 ms frame"),
        ("barge_in_min_speech_ms: 10\n", "at least one 20 ms frame"),
        ("tts_pre_delay_ms: -1\n", "cannot be negative"),
        ("barge_in: 'yes'\n", "true or false"),
        ("vad: webrtc\n", "vad must be"),
        ("tts_provider: piper\n", "tts_provider must be"),
        ("asr_model: whisper\n", "provider/model"),
        ("barge_in: [\n", "cannot read"),
    ],
)
def test_invalid_voice_yaml_is_refused(tmp_path: Path, document: str, message: str) -> None:
    (tmp_path / "voice.yaml").write_text(document)
    with pytest.raises(VoiceConfigError, match=message):
        load_pipeline_config(root=tmp_path)


@pytest.mark.parametrize(
    ("heard", "meant"),
    [
        ("See 1,042, postcode 92,000.", "C-1042, postcode 92000."),
        ("My customer number is C1,188 and my postcode is 75,005.", "C-1188"),
        ("c 1802 75006", "C-1802 75006"),
        ("C-1042 Postcode 92000", "C-1042 Postcode 92000"),
        ("I'd like 1,000,000 visits", "I'd like 1000000 visits"),
        ("  I need to cancel my visit.  ", "I need to cancel my visit."),
        ("see you on Friday", "see you on Friday"),
    ],
)
def test_transcripts_are_normalized_for_the_brain(heard: str, meant: str) -> None:
    assert meant in normalize_transcript(heard)


def test_cli_reads_the_fault_overlay_and_the_stack_overrides() -> None:
    options = options_from(
        parse_args(
            ["--brain=rules", "--fault=F7", "--vad=energy", "--asr=fake/tag", "--tts=synthetic"]
        )
    )
    assert options.agent.brain == "rules"
    assert options.pipeline.variant == "F7"
    assert options.pipeline.tts_pre_delay_ms == 700
    stack = speech_stack(options.pipeline, options.models_dir)
    assert isinstance(stack.asr, FakeASR)
    assert stack.tts.name == SyntheticTTS().name
    assert isinstance(stack.vad(), EnergyVad)


def test_cli_builds_silero_and_keeps_the_file_without_overrides(tmp_path: Path) -> None:
    options = options_from(parse_args(["--model=ollama/mistral"]))
    assert options.agent.model == "ollama/mistral"
    assert options.pipeline == load_pipeline_config()
    stack = speech_stack(
        options.pipeline.with_overrides(vad=None, asr_model="fake/tag", tts_provider="synthetic"),
        tmp_path,
    )
    assert isinstance(stack.vad(), SileroVad)


def test_cli_reports_configuration_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--fault=F1"]) == 2
    assert "not a voice fault variant" in capsys.readouterr().err


def test_voice_yaml_documents_every_setting() -> None:
    keys = set(yaml.safe_load((PROJECT_ROOT / VOICE_FILE).read_text()))
    assert keys == set(PipelineConfig.__dataclass_fields__) - {"variant"}
