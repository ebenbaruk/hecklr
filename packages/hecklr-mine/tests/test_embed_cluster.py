"""Stage 4 (WS13 cluster): embeddings, HDBSCAN groups and cluster labels."""

from decimal import Decimal
from typing import Any

import anyio
import httpx2
import numpy as np
import openai
import pytest

from hecklr_llm import FakeLLM, LLMOutputError
from hecklr_mine import HashingEmbedder, MineError, OpenAIEmbedder
from hecklr_mine.cluster import group_failures, keyword_label, llm_label, make_cluster
from hecklr_mine.embed import tokens
from hecklr_schemas import LLMRequest


def test_ws13_tokens_drop_stopwords_digits_and_read_placeholders() -> None:
    assert tokens("I live at [ADDRESS_2], not 12 Oak Lane!") == ["live", "address", "oak", "lane"]


def test_ws13_hashing_embedder_is_deterministic_normalized_and_semantic() -> None:
    embedder = HashingEmbedder(dimensions=512)
    texts = [
        "wrong address booked for the boiler repair",
        "the boiler repair was booked at the wrong address",
        "refund for a duplicate invoice payment",
        "",
    ]
    first = anyio.run(embedder.embed, texts)
    assert np.array_equal(first, anyio.run(embedder.embed, texts))
    assert np.allclose(np.linalg.norm(first[:3], axis=1), 1.0)
    assert not first[3].any()
    assert first[0] @ first[1] > 0.5 > first[0] @ first[2]
    assert embedder.name == "hashing-512"
    assert embedder.spent_usd == 0


def _embedding_transport(calls: list[dict[str, Any]], status: int = 200) -> httpx2.MockTransport:
    def handle(request: httpx2.Request) -> httpx2.Response:
        body = __import__("json").loads(request.content)
        calls.append(body)
        if status != 200:
            return httpx2.Response(status, json={"error": {"message": "nope"}})
        data = [
            {"object": "embedding", "index": i, "embedding": [float(len(text)), 1.0]}
            for i, text in reversed(list(enumerate(body["input"])))
        ]
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "data": data,
                "model": body["model"],
                "usage": {"prompt_tokens": 1000 * len(body["input"]), "total_tokens": 0},
            },
        )

    return httpx2.MockTransport(handle)


def _client(transport: httpx2.MockTransport) -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(
        api_key="test", http_client=httpx2.AsyncClient(transport=transport), max_retries=0
    )


def test_ws13_openai_embedder_batches_orders_and_prices() -> None:
    calls: list[dict[str, Any]] = []
    embedder = OpenAIEmbedder(_client(_embedding_transport(calls)))
    embedder.BATCH = 2  # type: ignore[misc]  # a smaller batch to exercise batching
    vectors = anyio.run(embedder.embed, ["a", "bbb", ""])
    assert [call["input"] for call in calls] == [["a", "bbb"], [" "]]
    assert calls[0]["model"] == "text-embedding-3-small"
    expected = np.array([[1.0, 1.0], [3.0, 1.0], [1.0, 1.0]])
    assert np.allclose(vectors, expected / np.linalg.norm(expected, axis=1, keepdims=True))
    assert embedder.spent_usd == Decimal("0.00006")
    assert embedder.name == "openai/text-embedding-3-small"
    anyio.run(embedder.aclose)


def test_ws13_openai_embedder_errors_are_mine_errors() -> None:
    embedder = OpenAIEmbedder(_client(_embedding_transport([], status=401)))
    with pytest.raises(MineError, match="embedding request to openai/text-embedding-3-small"):
        anyio.run(embedder.embed, ["x"])
    assert anyio.run(OpenAIEmbedder(_client(_embedding_transport([]))).embed, []).shape == (0, 1)


def test_ws13_openai_embedder_refuses_unpriced_models() -> None:
    with pytest.raises(MineError, match="no price"):
        OpenAIEmbedder(api_key="k", model="text-embedding-9")


def _blobs(seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    centers = np.eye(3, 16) * 5
    points = [center + rng.normal(scale=0.3, size=16) for center in centers for _ in range(12)]
    # Three lone points in directions of their own: noise, not a cluster.
    points += list(np.eye(16)[[8, 11, 14]] * 5)
    return np.asarray(points)


def test_ws13_hdbscan_groups_are_sorted_and_centrally_ordered() -> None:
    groups = group_failures(_blobs(), min_cluster_size=5)
    assert len(groups) == 3
    assert [len(g.members) for g in groups] == sorted(
        (len(g.members) for g in groups), reverse=True
    )
    for group in groups:
        blob_members = [member for member in group.members if member < 36]
        assert len(blob_members) == 12  # a whole blob, never split
        assert len({member // 12 for member in blob_members}) == 1  # and never mixed
        assert group.members[0] < 36  # the most central member is a blob point
        assert group.exemplars(2) == group.members[:2]
    assert group_failures(_blobs(), min_cluster_size=5) == groups


def test_ws13_too_few_failures_form_no_cluster() -> None:
    assert group_failures(np.ones((3, 4)), min_cluster_size=5) == []


def test_ws13_keyword_label_names_over_represented_words() -> None:
    members = ["gas smell emergency", "gas smell kitchen"]
    everyone = [*members, "invoice refund", "invoice copy"]
    label = keyword_label(members, everyone, words=2)
    assert label.label == "failures about gas, smell"
    assert "2 failed conversations" in label.description


def test_ws13_llm_label_is_structured_and_shows_at_most_eight_exemplars() -> None:
    seen: list[LLMRequest] = []

    def responder(request: LLMRequest) -> dict[str, str]:
        seen.append(request)
        return {"label": "fails when the customer gives two addresses", "description": "Mixed."}

    summaries = [f"summary {i}" for i in range(12)]
    label = anyio.run(lambda: llm_label(summaries, FakeLLM(responder=responder), "fake/m", seed=2))
    assert label.label == "fails when the customer gives two addresses"
    prompt = seen[0].messages[0].content
    assert "<summary 8>" in prompt
    assert "<summary 9>" not in prompt
    assert seen[0].temperature == 0.0
    with pytest.raises(LLMOutputError):
        anyio.run(lambda: llm_label(summaries, FakeLLM(script=["{}"]), "fake/m"))


def test_ws13_make_cluster_builds_a_production_failure_cluster() -> None:
    label = keyword_label(["a b"], ["a b"])
    cluster = make_cluster(label, ["c1", "c2"], ["c2"])
    assert cluster.source == "production"
    assert cluster.size == 2
    assert cluster.exemplar_conversation_ids == ["c2"]
