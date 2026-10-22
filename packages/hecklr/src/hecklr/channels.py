"""The voice and email channel drivers of a run, built from ``hecklr.yaml`` (ADR 0007e).

Voice: the persona's voice profile picks the TTS (``voice.tts.provider``) and the impairment
chain; ``models.asr`` picks how the caller hears the agent (``faster-whisper/tiny.en`` when
unset: local and free, from the ``hecklr[local-voice]`` extra). ``fake/...`` ASR and the
``synthetic`` TTS exist for tests and calibration; cloud providers need their API key. Every
choice is checked here, before a trial starts, so a missing extra or key is a configuration
error naming what to install or set. Recordings go to the run's directory, where the HTML
report finds them.

Email: the adapter's target is kept so the webhook reply endpoint can be stopped when the run
ends (two runs in one process, as ``hecklr calibrate --aa`` makes, would otherwise collide on its
port).
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final

from hecklr_adapters import make_email_target, make_voice_target
from hecklr_adapters.email import WebhookEmailTarget
from hecklr_adapters.voice import WebSocketVoiceAdapterConfig
from hecklr_channels.email import WEBHOOK_AGENT_ADDR, EmailDriver
from hecklr_schemas import (
    ChannelDriver,
    EmailAdapterConfig,
    Persona,
    SmtpImapAdapterConfig,
    VoiceAdapterConfig,
    WebhookAdapterConfig,
)

DEFAULT_ASR: Final = "faster-whisper/tiny.en"
"""The caller's ears when ``models.asr`` is unset: local, free, 4.5% WER on clean speech."""
MODELS_DIR: Final = "models"
"""Local speech models, under the project's output directory."""
TTS_CACHE_DIR: Final = "cache/tts"
"""Synthesized caller speech, cached by (text, voice, settings), under the output directory."""
VOICE_EXTRA: Final = "pip install 'hecklr[voice]'"


class ChannelUnavailableError(Exception):
    """A channel the suite runs on cannot be set up here: a missing extra, key or provider."""


@dataclass(frozen=True, slots=True)
class BuiltDriver:
    """A channel driver and what must be released when the run ends."""

    driver: ChannelDriver
    close: Callable[[], None] | None = None


def make_voice_driver(
    config: VoiceAdapterConfig | WebSocketVoiceAdapterConfig,
    personas: Iterable[Persona],
    *,
    asr_model: str | None,
    output_dir: Path,
    run_dir: Path,
) -> BuiltDriver:
    """The voice driver over the configured adapter, with every persona's voice checked.

    Raises:
        ChannelUnavailableError: ``hecklr-voice`` is not installed, or a persona's TTS, line
            codec or noise, or the ASR, cannot work on this machine (extra, key, ffmpeg).
        hecklr_adapters.AdapterConfigError: the voice adapter cannot work.
    """
    try:
        # Imported here: hecklr-voice is an optional extra and a chat or email run never needs it.
        import hecklr_voice as voice
    except ImportError as exc:
        raise ChannelUnavailableError(
            f"the voice channel needs the hecklr-voice engine: {VOICE_EXTRA}"
        ) from exc
    target = make_voice_target(config)
    models_dir = output_dir / MODELS_DIR
    model = asr_model or DEFAULT_ASR
    try:
        asr = voice.make_asr(model, models_dir=models_dir)
        for profile in [persona.voice for persona in personas if persona.voice is not None]:
            voice.make_tts(profile.tts.provider, models_dir=models_dir)
            voice.ImpairmentChain.for_voice(profile)
    except voice.VoiceConfigError as exc:
        hint = "" if asr_model else f" (models.asr is unset, so the caller hears with {model})"
        raise ChannelUnavailableError(f"the voice channel cannot run: {exc}{hint}") from exc
    tts = partial(voice.make_tts, cache_dir=output_dir / TTS_CACHE_DIR, models_dir=models_dir)
    driver: ChannelDriver = voice.VoiceDriver(target, asr=asr, tts=tts, run_dir=run_dir)
    return BuiltDriver(driver)


def make_email_driver(config: EmailAdapterConfig) -> BuiltDriver:
    """The email driver over the configured adapter, with its deadline and addresses.

    ``hecklr_channels.make_email_driver`` builds the same driver but keeps the target to
    itself; the run needs the webhook target to stop its reply endpoint.

    Raises:
        hecklr_adapters.AdapterConfigError: the email adapter cannot work.
    """
    target = make_email_target(config)
    match config:
        case SmtpImapAdapterConfig():
            driver = EmailDriver(
                target,
                agent_addr=config.inbox,
                customer_domain=config.from_domain,
                reply_timeout_s=config.reply_timeout_s,
            )
        case WebhookAdapterConfig():
            driver = EmailDriver(
                target, agent_addr=WEBHOOK_AGENT_ADDR, reply_timeout_s=config.timeout_s
            )
    close = target.close if isinstance(target, WebhookEmailTarget) else None
    return BuiltDriver(driver, close)
