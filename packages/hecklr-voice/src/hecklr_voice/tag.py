"""A text tag hidden in the least significant bit of 16-bit audio.

``SyntheticTTS`` stamps what it said into its own audio and ``FakeASR`` reads it back, so unit
tests get exact transcripts of agent speech without a speech model. The tag sits 90 dB below full
scale, is repeated end to end so any cut of the audio still holds a copy, and is protected by a
CRC so silence or real speech never decodes to a phantom transcript. Any lossy processing
(impairments, codecs, resampling) destroys it, which is fine: it only has to survive a clean link.
"""

import binascii
from typing import Final

import numpy as np
import numpy.typing as npt
from numpy.lib.stride_tricks import sliding_window_view

from hecklr_voice.audio import Pcm16

_SYNC: Final = 0xB5A3
_HEADER_BITS: Final = 32
_CRC_BITS: Final = 16
_MAX_PAYLOAD: Final = 0xFFFF

Bits = npt.NDArray[np.uint8]


def _word_bits(value: int) -> Bits:
    return np.array([(value >> shift) & 1 for shift in range(15, -1, -1)], dtype=np.uint8)


def _bits_value(bits: Bits) -> int:
    return int("".join(str(int(bit)) for bit in bits), 2)


def _frame(text: str) -> Bits:
    payload = text.encode("utf-8")
    if len(payload) > _MAX_PAYLOAD:
        raise ValueError(f"a tag holds at most {_MAX_PAYLOAD} bytes, got {len(payload)}")
    body = np.unpackbits(np.frombuffer(payload, dtype=np.uint8))
    crc = binascii.crc_hqx(payload, 0)
    return np.concatenate([_word_bits(_SYNC), _word_bits(len(payload)), body, _word_bits(crc)])


def embed_tag(pcm: Pcm16, text: str) -> Pcm16:
    """Return ``pcm`` with ``text`` repeated in its least significant bits.

    Audio too short for one whole copy is returned unchanged: a partial tag could never be read.
    """
    frame = _frame(text)
    if len(frame) > len(pcm):
        return pcm
    bits = np.resize(frame, len(pcm)).astype(np.int16)
    return np.bitwise_or(np.bitwise_and(pcm, np.int16(-2)), bits).astype(np.int16)


def extract_tag(pcm: Pcm16) -> str | None:
    """The first intact tag in ``pcm``, or ``None`` if there is none."""
    bits = np.bitwise_and(pcm, 1).astype(np.uint8)
    if len(bits) < _HEADER_BITS + _CRC_BITS:
        return None
    weights = 1 << np.arange(15, -1, -1, dtype=np.int64)
    words = sliding_window_view(bits, 16) @ weights
    for start in np.flatnonzero(words == _SYNC):
        text = _read_frame(bits, int(start))
        if text is not None:
            return text
    return None


def _read_frame(bits: Bits, start: int) -> str | None:
    length = _bits_value(bits[start + 16 : start + _HEADER_BITS])
    body_end = start + _HEADER_BITS + 8 * length
    if body_end + _CRC_BITS > len(bits):
        return None
    payload = np.packbits(bits[start + _HEADER_BITS : body_end]).tobytes()
    if binascii.crc_hqx(payload, 0) != _bits_value(bits[body_end : body_end + _CRC_BITS]):
        return None
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
