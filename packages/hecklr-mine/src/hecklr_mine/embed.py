"""Text embeddings for clustering failures and deduplicating scenarios.

Two implementations of one ``Embedder`` protocol: a free, offline ``HashingEmbedder`` (hashed
word and word-pair counts) and ``OpenAIEmbedder`` (``text-embedding-3-small``). Every vector is
L2-normalized, so a dot product is the cosine similarity.
"""

import hashlib
import itertools
import math
import re
from collections import Counter
from collections.abc import Sequence
from decimal import Decimal
from typing import Final, Protocol

import numpy as np
import numpy.typing as npt
import openai

from hecklr_mine.detect import STOPWORDS
from hecklr_mine.errors import MineError

Vectors = npt.NDArray[np.float64]
"""One L2-normalized row per text."""


class Embedder(Protocol):
    """Turns texts into comparable vectors."""

    @property
    def name(self) -> str:
        """What produced the vectors, recorded with the clusters, e.g. ``hashing-2048``."""
        ...

    @property
    def spent_usd(self) -> Decimal:
        """Money spent so far."""
        ...

    async def embed(self, texts: Sequence[str]) -> Vectors:
        """One normalized row per text, in order."""
        ...


def normalize_rows(vectors: Vectors) -> Vectors:
    """Scale every non-zero row to unit length."""
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return np.divide(vectors, norms, out=np.zeros_like(vectors), where=norms > 0)


_TOKEN = re.compile(r"\[([A-Z]+)_\d+\]|[A-Za-z0-9]+(?:'[A-Za-z]+)?")


def tokens(text: str) -> list[str]:
    """Content words of ``text``; a placeholder such as ``[ADDRESS_2]`` counts as ``address``."""
    found = []
    for match in _TOKEN.finditer(text):
        word = (match.group(1) or match.group()).casefold()
        if word not in STOPWORDS and not word.isdigit():
            found.append(word)
    return found


class HashingEmbedder:
    """Free and offline: hashed counts of words and adjacent word pairs, log-scaled.

    Stateless, so vectors from separate calls are comparable. Hashing uses BLAKE2 (Python's
    ``hash`` is salted per process) and a sign bit, so collisions cancel out on average.
    """

    def __init__(self, dimensions: int = 2048) -> None:
        self.dimensions = dimensions

    @property
    def name(self) -> str:
        """``hashing-<dimensions>``."""
        return f"hashing-{self.dimensions}"

    @property
    def spent_usd(self) -> Decimal:
        """Always zero."""
        return Decimal(0)

    async def embed(self, texts: Sequence[str]) -> Vectors:
        """Hash every text; no I/O."""
        vectors = np.zeros((len(texts), self.dimensions))
        for row, text in enumerate(texts):
            words = tokens(text)
            features = Counter(words + [f"{a} {b}" for a, b in itertools.pairwise(words)])
            for feature, count in features.items():
                digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % self.dimensions
                sign = 1.0 if digest[4] & 1 else -1.0
                vectors[row, index] += sign * (1.0 + math.log(count))
        return normalize_rows(vectors)


OPENAI_EMBEDDING_PRICES: Final = {
    "text-embedding-3-small": Decimal("0.02"),
    "text-embedding-3-large": Decimal("0.13"),
}
"""USD per million input tokens (list prices, 2026-10). Embeddings have no output tokens, so they
are priced here rather than in ``hecklr_llm``'s chat-model table."""


class OpenAIEmbedder:
    """OpenAI's embeddings endpoint (or any compatible ``base_url``)."""

    BATCH: Final = 256

    def __init__(
        self,
        client: openai.AsyncOpenAI | None = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "text-embedding-3-small",
    ) -> None:
        """``client`` is injected in tests; otherwise one is built from ``api_key``/``base_url``.

        Raises:
            MineError: ``model`` has no known price, so its spend could not be counted.
        """
        if model not in OPENAI_EMBEDDING_PRICES:
            raise MineError(
                f"no price is known for embedding model {model!r}; "
                f"known: {sorted(OPENAI_EMBEDDING_PRICES)}"
            )
        self._client = client or openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._spent = Decimal(0)

    @property
    def name(self) -> str:
        """``openai/<model>``."""
        return f"openai/{self._model}"

    @property
    def spent_usd(self) -> Decimal:
        """Cost of the tokens embedded so far."""
        return self._spent

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self._client.close()

    async def embed(self, texts: Sequence[str]) -> Vectors:
        """Embed in batches of ``BATCH``; empty texts are sent as a single space.

        Raises:
            MineError: The API refused or could not be reached.
        """
        rows: list[list[float]] = []
        for start in range(0, len(texts), self.BATCH):
            batch = [text or " " for text in texts[start : start + self.BATCH]]
            try:
                response = await self._client.embeddings.create(input=batch, model=self._model)
            except openai.OpenAIError as exc:
                raise MineError(f"embedding request to {self.name} failed: {exc}") from exc
            ordered = sorted(response.data, key=lambda item: item.index)
            rows.extend(item.embedding for item in ordered)
            price = OPENAI_EMBEDDING_PRICES[self._model]
            self._spent += Decimal(response.usage.prompt_tokens) * price / Decimal(1_000_000)
        if not rows:
            return np.zeros((0, 1))
        return normalize_rows(np.asarray(rows, dtype=np.float64))
