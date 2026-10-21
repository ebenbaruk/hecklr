import json

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import JsonValue

from hecklr_adapters import AdapterConfigError, AgentError
from hecklr_adapters.templates import (
    check_path,
    check_template,
    extract,
    extract_text,
    placeholders,
    render,
)


def test_render_substitutes_placeholders_anywhere_in_the_structure() -> None:
    template: JsonValue = {
        "input": {"text": "{{message}}", "meta": ["{{ trial_id }}", 3, None, True]},
        "conversation": "conv-{{session_id}}",
    }
    rendered = render(template, {"message": "hi", "trial_id": "T1", "session_id": "S1"})
    assert rendered == {
        "input": {"text": "hi", "meta": ["T1", 3, None, True]},
        "conversation": "conv-S1",
    }


@given(st.text())
def test_render_keeps_any_message_valid_json(message: str) -> None:
    rendered = render({"m": "{{message}}"}, {"message": message})
    assert json.loads(json.dumps(rendered)) == {"m": message}


def test_placeholders_lists_every_name() -> None:
    assert placeholders({"a": ["{{message}} {{x}}"], "b": 1}) == {"message", "x"}


def test_check_template_rejects_unknown_placeholders_and_missing_message() -> None:
    check_template({"q": "{{message}}"})
    with pytest.raises(AdapterConfigError, match="unknown template placeholder"):
        check_template({"q": "{{message}} {{user}}"})
    with pytest.raises(AdapterConfigError, match=r"must contain \{\{message\}\}"):
        check_template({"q": "{{session_id}}"})


def test_extract_follows_keys_and_list_indexes() -> None:
    data: JsonValue = {"choices": [{"message": {"content": "hello"}}]}
    assert extract(data, "choices.0.message") == {"content": "hello"}
    assert extract_text(data, "choices.0.message.content") == "hello"


@pytest.mark.parametrize("path", ["choices.1.message", "choices.x", "missing", "choices.0.a"])
def test_extract_reports_a_missing_step_as_an_agent_error(path: str) -> None:
    with pytest.raises(AgentError, match="has no"):
        extract({"choices": [{"message": {}}]}, path)


def test_extract_text_requires_a_string() -> None:
    with pytest.raises(AgentError, match="is int, not text"):
        extract_text({"n": 3}, "n")


@pytest.mark.parametrize("path", ["", "a..b", ".a", "a."])
def test_check_path_rejects_empty_segments(path: str) -> None:
    with pytest.raises(AdapterConfigError, match="empty segment"):
        check_path(path)
