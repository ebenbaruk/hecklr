"""Errors of the voice engine."""


class VoiceConfigError(ValueError):
    """A voice setting cannot work: unknown noise or codec, missing key or extra, bad range.

    Raised before audio is produced, so a misconfigured persona fails loudly instead of being
    silently tested without the impairment it asked for.
    """


class VoiceProviderError(RuntimeError):
    """A speech provider (TTS or ASR) failed: HTTP error, malformed audio or a model fault.

    Hecklr's own speech stack failing is a harness failure, never the agent's fault.
    """
