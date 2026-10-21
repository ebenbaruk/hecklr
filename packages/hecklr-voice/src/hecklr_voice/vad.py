"""Voice activity detection on the agent's incoming 20 ms frames.

``EnergyVad`` (the default) is deterministic and costs one RMS per frame, so it runs inline as
frames arrive. ``SileroVad`` (extra ``local``) is the robust choice for noisy agent audio; its
ONNX inference runs on a worker thread (onnxruntime releases the GIL), never on the event loop.
"""

import threading
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

import anyio.to_thread
import numpy as np

from hecklr_voice.audio import SAMPLE_RATE, Pcm16, frame_dbfs
from hecklr_voice.models import DEFAULT_MODELS_DIR, ensure_model_file, onnx_session, require_extra

DEFAULT_ENERGY_THRESHOLD_DBFS: Final = -45.0
SILERO_URL: Final = (
    "https://github.com/snakers4/silero-vad/raw/v6.2/src/silero_vad/data/silero_vad.onnx"
)
_SILERO_WINDOW: Final = 512
_SILERO_CONTEXT: Final = 64


@runtime_checkable
class Vad(Protocol):
    """Classifies consecutive 16 kHz frames of one stream; stateful, so one per call."""

    async def prepare(self) -> None:
        """Load whatever the VAD needs before the call starts (frames must not wait on it)."""
        ...

    async def is_speech(self, frame: Pcm16) -> bool:
        """Whether this frame contains speech."""
        ...


VadFactory = Callable[[], Vad]


class EnergyVad:
    """Speech when the frame's RMS level reaches ``threshold_dbfs``."""

    def __init__(self, threshold_dbfs: float = DEFAULT_ENERGY_THRESHOLD_DBFS) -> None:
        self._threshold = threshold_dbfs

    async def prepare(self) -> None:
        """Nothing to load."""

    async def is_speech(self, frame: Pcm16) -> bool:
        """Compare the frame level to the threshold."""
        return frame_dbfs(frame) >= self._threshold


class SileroVad:
    """Silero VAD v5+ through onnxruntime: 512-sample windows with a 64-sample context.

    Frames are buffered into windows; a frame's verdict is the speech probability of the last
    complete window, so the decision lags the audio by at most one window (32 ms). Pass
    ``session`` to reuse a loaded model; otherwise it is downloaded and loaded on first use.
    """

    def __init__(
        self,
        *,
        threshold: float = 0.5,
        models_dir: Path = DEFAULT_MODELS_DIR,
        session: Any = None,  # an onnxruntime.InferenceSession
    ) -> None:
        if session is None:
            require_extra("onnxruntime", "Silero VAD")
        self._threshold = threshold
        self._path = models_dir / "silero" / "silero_vad.onnx"
        self._session = session
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, _SILERO_CONTEXT), dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._probability = 0.0

    async def prepare(self) -> None:
        """Download the model if needed and load the process-wide session."""
        if self._session is None:
            path = await ensure_model_file(SILERO_URL, self._path)
            self._session = await anyio.to_thread.run_sync(_shared_session, path)

    async def is_speech(self, frame: Pcm16) -> bool:
        """Buffer the frame and score every complete window on a worker thread."""
        await self.prepare()
        self._pending = np.concatenate([self._pending, frame.astype(np.float32) / 32768.0])
        if len(self._pending) >= _SILERO_WINDOW:
            await anyio.to_thread.run_sync(self._score_windows)
        return self._probability >= self._threshold

    def _score_windows(self) -> None:
        while len(self._pending) >= _SILERO_WINDOW:
            window = self._pending[:_SILERO_WINDOW][np.newaxis, :]
            self._pending = self._pending[_SILERO_WINDOW:]
            batch = np.concatenate([self._context, window], axis=1)
            inputs = {
                "input": batch,
                "state": self._state,
                "sr": np.array(SAMPLE_RATE, dtype=np.int64),
            }
            output, self._state = self._session.run(None, inputs)
            self._context = batch[:, -_SILERO_CONTEXT:]
            self._probability = float(np.asarray(output).reshape(-1)[0])


_SESSION_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def _load_shared(path: Path) -> Any:  # an onnxruntime.InferenceSession
    return onnx_session(path, 1)


def _shared_session(path: Path) -> Any:  # an onnxruntime.InferenceSession
    """The process-wide Silero session, loaded once.

    The recurrent state is an input, so calls can share one session; loading holds the GIL long
    enough to stall every call's frames, so it happens once, before calls start.
    """
    with _SESSION_LOCK:
        return _load_shared(path)
