import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from hecklr_schemas import (
    TRIAL_ID_HEADER,
    AgentChunk,
    AudioLink,
    Channel,
    ChatSession,
    ChatTarget,
    EmailAttachment,
    EmailMessage,
    EmailTarget,
    Grade,
    GradeContext,
    GradeId,
    Grader,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Message,
    Persona,
    Scenario,
    SessionContext,
    SessionSummary,
    TimedFrame,
    Trial,
    TrialId,
    VoiceTarget,
    WorldSnapshot,
    authoring_json_schemas,
    new_id,
    write_json_schemas,
)

# --- minimal implementations: if these type-check, the Protocols are implementable ----------


class EchoSession:
    def __init__(self) -> None:
        self.turns = 0

    async def send(self, text: str) -> AsyncIterator[AgentChunk]:
        self.turns += 1
        for word in text.split():
            yield AgentChunk(text=word + " ")
        yield AgentChunk(final=True)

    async def close(self) -> SessionSummary:
        return SessionSummary(turns=self.turns)


class EchoTarget:
    async def open(self, ctx: SessionContext) -> ChatSession:
        return EchoSession()


class SilentLink:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send_frame(self, pcm: bytes, sample_rate: int) -> None:
        self.sent.append(pcm)

    async def frames(self) -> AsyncIterator[TimedFrame]:
        yield TimedFrame(pcm=b"\x00\x00" * 320, sample_rate=16_000, sample_index=0, t_ms=0)

    async def hangup(self) -> None:
        self.sent.clear()


class SilentVoiceAgent:
    async def connect(self, ctx: SessionContext) -> AudioLink:
        return SilentLink()


class AutoReplyInbox:
    def __init__(self) -> None:
        self.inbox: list[EmailMessage] = []

    async def deliver(self, msg: EmailMessage, ctx: SessionContext) -> None:
        self.inbox.append(msg)

    async def replies(self, ctx: SessionContext) -> AsyncIterator[EmailMessage]:
        for msg in self.inbox:
            yield msg.model_copy(
                update={
                    "message_id": "<reply@acme.test>",
                    "in_reply_to": msg.message_id,
                    "references": [msg.message_id],
                    "subject": f"Re: {msg.subject}",
                    "from_addr": msg.to_addrs[0],
                    "to_addrs": [msg.from_addr],
                }
            )


class AlwaysPass:
    name = "always_pass"
    version = "1"

    async def grade(self, ctx: GradeContext) -> Grade:
        return Grade(
            id=GradeId(new_id()),
            trial_id=ctx.trial.id,
            grader=self.name,
            grader_version=self.version,
            value=True,
            passed=True,
        )


class ConstantLLM:
    async def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(model=request.model, text="ok")


def _ctx() -> SessionContext:
    return SessionContext(trial_id=TrialId(new_id()), seed=7)


@pytest.mark.anyio
async def test_chat_protocol_streams_until_final_chunk() -> None:
    target: ChatTarget = EchoTarget()
    session = await target.open(_ctx())

    chunks = [chunk async for chunk in session.send("move my visit")]
    summary = await session.close()

    assert "".join(c.text for c in chunks).split() == ["move", "my", "visit"]
    assert chunks[-1].final
    assert summary.turns == 1


@pytest.mark.anyio
async def test_voice_protocol_exchanges_frames() -> None:
    agent: VoiceTarget = SilentVoiceAgent()
    link = await agent.connect(_ctx())

    await link.send_frame(b"\x00\x00" * 320, 16_000)
    frames = [frame async for frame in link.frames()]
    await link.hangup()

    assert frames[0].duration_ms == pytest.approx(20.0)


@pytest.mark.anyio
async def test_email_protocol_threads_replies() -> None:
    agent: EmailTarget = AutoReplyInbox()
    sent = EmailMessage(
        message_id="<1@customers.hecklr.test>",
        subject="Boiler visit",
        body="Can we move it?",
        from_addr="camille@customers.hecklr.test",
        to_addrs=["support@acme.test"],
    )

    await agent.deliver(sent, _ctx())
    replies = [reply async for reply in agent.replies(_ctx())]

    assert replies[0].in_reply_to == sent.message_id
    assert replies[0].to_addrs == ["camille@customers.hecklr.test"]


def test_implementations_satisfy_runtime_checkable_protocols() -> None:
    assert isinstance(EchoTarget(), ChatTarget)
    assert isinstance(EchoSession(), ChatSession)
    assert isinstance(SilentVoiceAgent(), VoiceTarget)
    assert isinstance(SilentLink(), AudioLink)
    assert isinstance(AutoReplyInbox(), EmailTarget)
    assert isinstance(AlwaysPass(), Grader)
    assert isinstance(ConstantLLM(), LLMProvider)


def test_session_context_carries_the_correlation_header() -> None:
    ctx = _ctx()

    assert TRIAL_ID_HEADER == "X-Hecklr-Trial-Id"
    assert ctx.headers() == {"X-Hecklr-Trial-Id": ctx.trial_id}


def test_email_attachments_are_raw_bytes_in_python_and_base64_in_json() -> None:
    attachment = EmailAttachment(filename="invoice.pdf", data=b"%PDF-\x00\xff")

    as_json = json.loads(attachment.model_dump_json())

    assert as_json["data"] == "JVBERi0A/w=="
    assert EmailAttachment.model_validate_json(attachment.model_dump_json()) == attachment


def test_email_attachment_rejects_invalid_base64() -> None:
    with pytest.raises(ValidationError):
        EmailAttachment.model_validate_json('{"filename": "x", "data": "not base64!"}')


@pytest.mark.parametrize("address", ["no-at-sign", "two@@x.test", "a b@x.test"])
def test_email_addresses_are_checked(address: str) -> None:
    with pytest.raises(ValidationError):
        EmailMessage(message_id="<1@x>", subject="s", body="b", from_addr=address, to_addrs=["a@b"])


@pytest.mark.anyio
async def test_grader_reads_the_grade_context() -> None:
    snapshot = WorldSnapshot(world_id="w", taken_at_ms=0, collections={})
    trial = Trial(
        id=TrialId(new_id()),
        run_id=new_id(),  # type: ignore[arg-type]  # pydantic validates the str at runtime
        scenario_id="s",
        persona_id="p",
        channel=Channel.CHAT,
        index=0,
        seed=1,
        status="ok",
        started_at=datetime(2026, 10, 21, tzinfo=UTC),
        duration_ms=1200,
        cost_usd=Decimal(0),
        world_before=snapshot,
        world_after=snapshot,
    )
    ctx = GradeContext(
        trial=trial,
        events=[],
        scenario=Scenario.model_validate(
            {
                "id": "s",
                "title": "t",
                "channels": ["chat"],
                "persona": "p",
                "world": "w",
                "goal": "g",
                "success": [{"type": "custom", "name": "always_pass"}],
            }
        ),
        persona=Persona(id="p", description="d"),
        llm=ConstantLLM(),
    )

    grade = await AlwaysPass().grade(ctx)

    assert grade.passed
    assert grade.trial_id == trial.id


def test_llm_request_validates_its_contract() -> None:
    request = LLMRequest(model="fake/judge", messages=[Message(role="user", content="hi")])

    assert request.temperature is None
    assert request.max_tokens == 1024
    with pytest.raises(ValidationError):
        LLMRequest(model="judge", messages=[Message(role="user", content="hi")])
    with pytest.raises(ValidationError):
        LLMRequest(model="fake/judge", messages=[])


def test_json_schemas_cover_every_authored_file(tmp_path: Path) -> None:
    schemas = authoring_json_schemas()

    assert set(schemas) == {"hecklr-config", "scenario", "persona", "world"}
    scenario = schemas["scenario"]
    assert scenario["$id"] == "https://hecklr.dev/schemas/scenario.schema.json"
    assert scenario["additionalProperties"] is False
    assert "success" in scenario["required"]  # type: ignore[operator]  # dict from JSON

    paths = write_json_schemas(tmp_path / "schemas")
    assert sorted(p.name for p in paths) == [
        "hecklr-config.schema.json",
        "persona.schema.json",
        "scenario.schema.json",
        "world.schema.json",
    ]
    assert json.loads(paths[0].read_text())["$schema"].endswith("2020-12/schema")
