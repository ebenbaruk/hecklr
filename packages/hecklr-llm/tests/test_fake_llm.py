from collections.abc import Callable

import anyio
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, Field, JsonValue

from hecklr_llm import (
    FakeLLM,
    FakeScriptExhaustedError,
    LLMOutputError,
    LLMRequest,
    Message,
    minimal_instance,
)

RequestFactory = Callable[..., LLMRequest]


class Verdict(BaseModel):
    verdict: bool
    rationale: str = Field(min_length=3)
    evidence: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0.5, le=1.0)
    label: str = "resolved"


@pytest.mark.anyio
async def test_same_request_gets_the_same_reply_across_instances(
    make_request: RequestFactory,
) -> None:
    request = make_request("move my visit", seed=7)

    first = await FakeLLM().complete(request)
    second = await FakeLLM().complete(request)

    assert first == second
    assert first.text.startswith("fake reply ")


@pytest.mark.anyio
async def test_seed_and_content_change_the_default_reply(make_request: RequestFactory) -> None:
    fake = FakeLLM()

    base = await fake.complete(make_request("a", seed=1))
    other_seed = await fake.complete(make_request("a", seed=2))
    other_text = await fake.complete(make_request("b", seed=1))

    assert len({base.text, other_seed.text, other_text.text}) == 3


@given(
    text=st.text(min_size=1, max_size=200),
    seed=st.none() | st.integers(),
    system=st.none() | st.text(max_size=50),
)
def test_fake_llm_is_a_pure_function_of_the_request(
    text: str, seed: int | None, system: str | None
) -> None:
    request = LLMRequest(
        model="fake/sim", messages=[Message(role="user", content=text)], seed=seed, system=system
    )

    async def twice() -> tuple[str, str]:
        return (await FakeLLM().complete(request)).text, (await FakeLLM().complete(request)).text

    first, second = anyio.run(twice)
    assert first == second


@pytest.mark.anyio
async def test_rules_match_the_last_message_in_order(make_request: RequestFactory) -> None:
    fake = FakeLLM(rules=[(r"refund", "No refunds."), (r"(?i)hello", "Hi there!")])

    assert (await fake.complete(make_request("Hello, I want a refund"))).text == "No refunds."
    assert (await fake.complete(make_request("hello"))).text == "Hi there!"
    assert (await fake.complete(make_request("bye"))).text.startswith("fake reply")


@pytest.mark.anyio
async def test_responder_sees_the_whole_request(make_request: RequestFactory) -> None:
    fake = FakeLLM(responder=lambda request: f"{request.model}:{request.seed}")

    reply = await fake.complete(make_request("x", model="fake/judge", seed=42))

    assert reply.text == "fake/judge:42"


@pytest.mark.anyio
async def test_script_replies_in_order_then_fails_loudly(make_request: RequestFactory) -> None:
    fake = FakeLLM(script=["one", {"two": 2}])

    assert (await fake.complete(make_request())).text == "one"
    assert (await fake.complete(make_request())).text == '{"two": 2}'
    with pytest.raises(FakeScriptExhaustedError, match="2 replies"):
        await fake.complete(make_request())


@pytest.mark.anyio
async def test_model_replies_are_serialized_and_parsed(make_request: RequestFactory) -> None:
    verdict = Verdict(verdict=True, rationale="booked", evidence=["e1"], confidence=0.9)
    fake = FakeLLM(script=[verdict])

    reply = await fake.complete(make_request(json_schema=Verdict.model_json_schema()))

    assert Verdict.model_validate_json(reply.text) == verdict
    assert reply.parsed == verdict.model_dump(mode="json")


@pytest.mark.anyio
async def test_default_structured_reply_satisfies_the_schema(make_request: RequestFactory) -> None:
    reply = await FakeLLM().complete(make_request(json_schema=Verdict.model_json_schema()))

    parsed = Verdict.model_validate_json(reply.text)
    assert parsed.label == "resolved"
    assert parsed.confidence == 0.5


@pytest.mark.anyio
async def test_non_json_reply_to_a_schema_request_is_an_output_error(
    make_request: RequestFactory,
) -> None:
    fake = FakeLLM(script=["not json"])

    with pytest.raises(LLMOutputError, match="not valid JSON"):
        await fake.complete(make_request(json_schema={"type": "object"}))


@pytest.mark.anyio
async def test_calls_are_recorded_with_estimated_usage_and_zero_cost(
    make_request: RequestFactory,
) -> None:
    fake = FakeLLM(script=["12345678"])
    request = make_request("x" * 40)

    reply = await fake.complete(request)

    assert fake.calls == [request]
    assert reply.usage.input_tokens == 10
    assert reply.usage.output_tokens == 2
    assert reply.cost_usd == 0
    assert not reply.cached


class Address(BaseModel):
    street: str
    postcode: str = Field(min_length=5)


class Customer(BaseModel):
    id: int = Field(gt=0)
    address: Address
    tags: list[str] = Field(min_length=2)
    tier: str | None
    balance: float = Field(le=-1)


def test_minimal_instance_follows_refs_combinators_and_bounds() -> None:
    instance = minimal_instance(Customer.model_json_schema())

    customer = Customer.model_validate(instance)
    assert customer.id == 1
    assert customer.address.postcode == "xxxxx"
    assert customer.tags == ["", ""]
    assert customer.tier == ""  # first anyOf branch
    assert customer.balance == -1


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"enum": ["a", "b"]}, "a"),
        ({"const": 3}, 3),
        ({"type": "boolean"}, False),
        ({"type": "null"}, None),
        ({"type": ["integer", "null"]}, 0),
        ({"type": "array"}, []),
        ({"type": "object", "properties": {"a": {"type": "string"}}}, {}),
        ({"allOf": [{"type": "integer", "exclusiveMinimum": 0}]}, 1),
    ],
)
def test_minimal_instance_scalars(schema: dict[str, object], expected: object) -> None:
    assert minimal_instance(schema) == expected  # type: ignore[arg-type]  # literal test schemas


@pytest.mark.parametrize(
    ("ref", "message"),
    [
        ("https://example.com/schema", "only local"),
        ("#/$defs/Missing", "unresolvable"),
        ("#/$defs/Leaf/type", "does not point at a schema"),
    ],
)
def test_minimal_instance_rejects_bad_refs(ref: str, message: str) -> None:
    schema: dict[str, JsonValue] = {"$ref": ref, "$defs": {"Leaf": {"type": "string"}}}

    with pytest.raises(ValueError, match=message):
        minimal_instance(schema)
