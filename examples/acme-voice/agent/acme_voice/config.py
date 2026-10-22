"""The voice pipeline's settings (``voice.yaml``), optionally overlaid by a voice fault variant.

The chat agent's behaviour lives in its prompt and tool catalog (``acme_agent``); the voice
agent adds one editable file, ``voice.yaml``: endpointing, barge-in, the delay before speaking,
and which ASR, VAD and TTS it runs. A voice fault variant (``examples/acme-faults/F7``..``F10``)
is an overlay directory holding a replacement ``voice.yaml`` with one line changed, and nothing
else but its README, so ``--fault F7`` changes exactly what the README says and no code path.
"""

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal, cast, get_args

import yaml

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
"""``examples/acme-voice``: where ``voice.yaml`` and ``hecklr.yaml`` live."""
FAULTS_ROOT: Final = PROJECT_ROOT.parent / "acme-faults"
VOICE_FILE: Final = Path("voice.yaml")
OVERLAY_FILES: Final = frozenset({"voice.yaml", "README.md"})
_FAULT_ID = re.compile(r"^F\d+$")

VadKind = Literal["energy", "silero"]
TtsProvider = Literal["kokoro", "synthetic"]


class VoiceConfigError(ValueError):
    """The voice agent cannot start: a bad setting, a missing file or an overlay with code."""


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """What ``voice.yaml`` sets; the field names are its keys."""

    end_of_speech_ms: int = 800
    barge_in: bool = True
    barge_in_min_speech_ms: int = 80
    tts_pre_delay_ms: int = 0
    asr_model: str = "faster-whisper/base.en"
    vad: VadKind = "silero"
    tts_provider: TtsProvider = "kokoro"
    tts_voice: str = "af_heart"
    variant: str = "clean"
    """``clean`` or the overlay directory's name (e.g. ``F7``); not a ``voice.yaml`` key."""

    def __post_init__(self) -> None:
        for name in ("end_of_speech_ms", "barge_in_min_speech_ms", "tts_pre_delay_ms"):
            if type(getattr(self, name)) is not int:
                raise VoiceConfigError(f"{name} must be a whole number of milliseconds")
        if type(self.barge_in) is not bool:
            raise VoiceConfigError("barge_in must be true or false")
        if self.end_of_speech_ms < 20:
            raise VoiceConfigError("end_of_speech_ms must be at least one 20 ms frame")
        if self.barge_in_min_speech_ms < 20:
            raise VoiceConfigError("barge_in_min_speech_ms must be at least one 20 ms frame")
        if self.tts_pre_delay_ms < 0:
            raise VoiceConfigError("tts_pre_delay_ms cannot be negative")
        if self.vad not in get_args(VadKind):
            raise VoiceConfigError(f"vad must be one of {get_args(VadKind)}, got {self.vad!r}")
        if self.tts_provider not in get_args(TtsProvider):
            known = get_args(TtsProvider)
            raise VoiceConfigError(
                f"tts_provider must be one of {known}, got {self.tts_provider!r}"
            )
        if "/" not in self.asr_model:
            raise VoiceConfigError(f"asr_model is provider/model, got {self.asr_model!r}")

    def with_overrides(
        self, *, vad: str | None, asr_model: str | None, tts_provider: str | None
    ) -> "PipelineConfig":
        """The configuration with command-line overrides applied (``None`` keeps the file's)."""
        return replace(
            self,
            vad=cast(VadKind, vad or self.vad),
            asr_model=asr_model or self.asr_model,
            tts_provider=cast(TtsProvider, tts_provider or self.tts_provider),
        )


_KEYS: Final = frozenset(PipelineConfig.__dataclass_fields__) - {"variant"}


def voice_fault_overlay(fault_id: str, faults_root: Path = FAULTS_ROOT) -> Path:
    """The overlay directory of fault variant ``fault_id`` (e.g. ``F7``)."""
    if not _FAULT_ID.match(fault_id):
        raise VoiceConfigError(f"fault ids look like F7..F10, got {fault_id!r}")
    overlay = faults_root / fault_id
    if not (overlay / VOICE_FILE).is_file():
        raise VoiceConfigError(f"{fault_id!r} in {faults_root} is not a voice fault variant")
    return overlay


def load_pipeline_config(root: Path = PROJECT_ROOT, overlay: Path | None = None) -> PipelineConfig:
    """Read ``voice.yaml``, from ``overlay`` when given.

    Raises:
        VoiceConfigError: the file is missing or malformed, or the overlay holds other files.
    """
    if overlay is not None:
        _check_overlay(overlay)
    path = (overlay if overlay is not None else root) / VOICE_FILE
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise VoiceConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise VoiceConfigError(f"{path}: expected a mapping of settings")
    if unknown := sorted(set(document) - _KEYS):
        raise VoiceConfigError(f"{path}: unknown settings {unknown}")
    variant = "clean" if overlay is None else overlay.name
    try:
        return PipelineConfig(**document, variant=variant)
    except TypeError as exc:
        raise VoiceConfigError(f"{path}: {exc}") from exc


def _check_overlay(overlay: Path) -> None:
    if not overlay.is_dir():
        raise VoiceConfigError(f"overlay {overlay} is not a directory")
    files = {str(path.relative_to(overlay)) for path in overlay.rglob("*") if path.is_file()}
    if unexpected := sorted(files - OVERLAY_FILES):
        raise VoiceConfigError(f"overlay {overlay} may only replace voice.yaml: {unexpected}")
