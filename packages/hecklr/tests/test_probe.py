"""Agent and Ollama detection over mocked HTTP."""

from collections.abc import Callable

import httpx2
import pytest

from hecklr.probe import ollama_models, probe_agent, reply_path

Handler = Callable[[httpx2.Request], httpx2.Response]


def _transport(routes: dict[str, httpx2.Response]) -> httpx2.MockTransport:
    def handle(request: httpx2.Request) -> httpx2.Response:
        return routes.get(str(request.url), httpx2.Response(404))

    return httpx2.MockTransport(handle)


@pytest.mark.parametrize(
    "url",
    ["http://gpu:8000", "http://gpu:8000/v1", "http://gpu:8000/v1/chat/completions"],
)
def test_openai_compatible_agent_is_detected_from_any_of_its_urls(url: str) -> None:
    routes = {
        "http://gpu:8000/v1/models": httpx2.Response(200, json={"data": [{"id": "support-bot"}]})
    }

    probe = probe_agent(url, transport=_transport(routes))

    assert (probe.adapter, probe.url, probe.reachable) == (
        "openai-compatible",
        "http://gpu:8000/v1",
        True,
    )
    assert probe.models == ("support-bot",)


def test_plain_http_endpoint_is_an_http_agent() -> None:
    routes = {"http://localhost:8000/chat": httpx2.Response(405, text="Method Not Allowed")}

    probe = probe_agent("http://localhost:8000/chat", transport=_transport(routes))

    assert (probe.adapter, probe.reachable) == ("http", True)
    assert "405" in probe.detail


def test_models_endpoint_that_is_not_json_is_ignored() -> None:
    routes = {
        "http://localhost:8000/v1/models": httpx2.Response(200, text="<html>"),
        "http://localhost:8000": httpx2.Response(200, text="ok"),
    }

    assert probe_agent("http://localhost:8000", transport=_transport(routes)).adapter == "http"


def test_unreachable_agent() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    probe = probe_agent("http://localhost:9/chat", transport=httpx2.MockTransport(refuse))

    assert (probe.adapter, probe.reachable) == ("http", False)
    assert "nothing answered" in probe.detail


def test_ollama_models_are_listed() -> None:
    tags = {"models": [{"name": "qwen3:8b"}, {"name": "llama3:8b"}, {"size": 1}]}
    routes = {"http://localhost:11434/api/tags": httpx2.Response(200, json=tags)}

    assert ollama_models(transport=_transport(routes)) == ["llama3:8b", "qwen3:8b"]


def test_no_ollama() -> None:
    assert ollama_models(transport=_transport({})) is None


@pytest.mark.parametrize(
    ("reply", "path"),
    [
        ({"session": "s", "reply": "hi"}, "reply"),
        ({"id": "x", "say": "hi"}, "id"),
        ({"count": 1}, None),
        (["hi"], None),
    ],
)
def test_reply_path_is_found_by_sending_one_message(reply: object, path: str | None) -> None:
    sent: list[bytes] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.content)
        return httpx2.Response(200, json=reply)

    assert reply_path("http://a/chat", transport=httpx2.MockTransport(handle)) == path
    assert b'"message"' in sent[0]


def test_reply_path_of_a_non_json_or_failing_agent() -> None:
    text = httpx2.MockTransport(lambda request: httpx2.Response(200, text="plain"))
    error = httpx2.MockTransport(lambda request: httpx2.Response(500, json={"reply": "x"}))

    assert reply_path("http://a/chat", transport=text) is None
    assert reply_path("http://a/chat", transport=error) is None
