"""The Pipecat stack agent's own logic: what it reads from the context and what it charges.

The pipelines themselves are exercised by real benchmark runs (bench/README.md); these tests
pin the parts a run cannot check: the brain's input and the cloud spend meter and cap.
"""

import importlib
import sys
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

AGENTS = Path(__file__).resolve().parents[4] / "bench" / "stacks" / "agents"


@pytest.fixture(scope="module")
def pipecat_acme() -> Iterator[ModuleType]:
    """The agent script as a module (its own ``_paths`` makes the reference agents importable)."""
    saved = list(sys.path)
    sys.path.insert(0, str(AGENTS))
    try:
        yield importlib.import_module("pipecat_acme")
    finally:
        sys.path[:] = saved


def test_the_brain_hears_the_newest_user_message(pipecat_acme: ModuleType) -> None:
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": [{"type": "text", "text": "cancel"}, {"text": "it"}]},
        {"role": "assistant", "content": "partial"},
    ]

    assert pipecat_acme.last_user_text(messages) == "cancel it"
    assert pipecat_acme.last_user_text([{"role": "assistant", "content": "x"}]) == ""


def test_local_speech_costs_nothing(pipecat_acme: ModuleType) -> None:
    meter = pipecat_acme.Meter("whisper", "kokoro", stt_seconds=3600, tts_seconds=600)

    assert meter.spent_usd == 0
    assert not meter.exhausted


def test_cloud_speech_is_charged_at_list_price_and_capped(pipecat_acme: ModuleType) -> None:
    meter = pipecat_acme.Meter("assemblyai", "openai", cap_usd=Decimal("0.20"))
    meter.stt_seconds = 3600.0  # one hour of streaming session: USD 0.15
    meter.tts_seconds = 120.0  # two minutes of speech: USD 0.03

    assert meter.spent_usd == Decimal("0.180000")
    assert not meter.exhausted
    meter.tts_seconds = 240.0

    assert meter.exhausted
