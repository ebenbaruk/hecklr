"""The LLM brain's tool loop, on FakeLLM (no network)."""

import json

import pytest
from acme_agent import AcmeAgent, AgentConfigError, AgentSettings, load_agent_files
from acme_agent.llm_brain import GAVE_UP, AgentStep, LLMBrain, step_seed, system_prompt
from acme_agent.tools import LocalWorldTools, ToolRunner
from acme_testing import files_for

from hecklr_llm import FakeLLM, LLMOutputError
from hecklr_schemas import LLMRequest, World

pytestmark = pytest.mark.anyio


def call(tool: str, **arguments: str | int | float | bool) -> AgentStep:
    return AgentStep(action="call_tool", tool=tool, arguments=arguments, message="")


def say(message: str) -> AgentStep:
    return AgentStep(action="reply", tool="", arguments={}, message=message)


def llm_agent(llm: FakeLLM, max_steps: int = 8) -> AcmeAgent:
    settings = AgentSettings(brain="llm", model="fake/agent", max_steps=max_steps)
    return AcmeAgent(load_agent_files(), settings, llm)


async def test_tool_calls_run_and_their_results_reach_the_next_step(world: World) -> None:
    llm = FakeLLM(
        script=[
            call("verify_customer", customer_id="C-1042", postcode="92000"),
            call("list_appointments", customer_id="C-1042"),
            say("Your boiler service is on Friday 23 October at 09:00."),
        ]
    )
    tools = LocalWorldTools(world)
    reply = await llm_agent(llm).respond("s", "When is my visit? C-1042, 92000", lambda: tools)
    assert reply.text == "Your boiler service is on Friday 23 October at 09:00."
    assert [step.call.tool for step in reply.steps] == ["verify_customer", "list_appointments"]
    assert reply.reported_steps
    last_prompt = llm.calls[-1].messages[0].content
    assert "Customer: When is my visit? C-1042, 92000" in last_prompt
    assert 'Tool call: list_appointments {"customer_id":"C-1042"}' in last_prompt
    assert '"slot":"2026-10-23T09:00"' in last_prompt


async def test_memory_spans_turns(world: World) -> None:
    llm = FakeLLM(script=[say("Hello! How can I help?"), say("Sure.")])
    agent = llm_agent(llm)
    tools = LocalWorldTools(world)
    await agent.respond("s", "Hi", lambda: tools)
    await agent.respond("s", "Move my visit", lambda: tools)
    assert (
        "You: Hello! How can I help?\nCustomer: Move my visit" in llm.calls[-1].messages[0].content
    )


async def test_tool_errors_are_fed_back_not_raised(world: World) -> None:
    llm = FakeLLM(
        script=[call("verify_customer", customer_id="C-1042", postcode="00000"), say("No.")]
    )
    reply = await llm_agent(llm).respond("s", "I'm C-1042", lambda: LocalWorldTools(world))
    assert reply.steps[0].result.error is not None
    assert '"code":"not_found"' in llm.calls[-1].messages[0].content


async def test_emergency_ticket_from_the_llm_is_a_handoff(world: World) -> None:
    llm = FakeLLM(
        script=[
            call("create_emergency_ticket", category="gas", description="smell of gas"),
            say("Please leave now; I'm transferring you to our on-call engineer."),
        ]
    )
    reply = await llm_agent(llm).respond("s", "I smell gas", lambda: LocalWorldTools(world))
    assert reply.handoff is not None
    assert reply.ended


async def test_the_step_limit_ends_with_an_apology(world: World) -> None:
    llm = FakeLLM(responder=lambda _: call("get_policy", policy_id="fees"))
    reply = await llm_agent(llm, max_steps=3).respond("s", "fees?", lambda: LocalWorldTools(world))
    assert reply.text == GAVE_UP
    assert len(reply.steps) == 3


async def test_an_empty_reply_is_replaced(world: World) -> None:
    llm = FakeLLM(script=[say("  ")])
    reply = await llm_agent(llm).respond("s", "hi", lambda: LocalWorldTools(world))
    assert reply.text == GAVE_UP


async def test_malformed_model_output_is_an_agent_failure(world: World) -> None:
    llm = FakeLLM(script=['{"action": "dance"}'])
    with pytest.raises(LLMOutputError):
        await llm_agent(llm).respond("s", "hi", lambda: LocalWorldTools(world))


async def test_requests_are_reproducible_and_carry_the_catalog(world: World) -> None:
    first, second = FakeLLM(script=[say("a")]), FakeLLM(script=[say("a")])
    for llm in (first, second):
        await llm_agent(llm).respond("s", "hi", lambda: LocalWorldTools(world))
    request: LLMRequest = first.calls[0]
    assert request == second.calls[0]
    assert request.seed == step_seed(0, 1, 0)
    assert request.temperature == 0.0
    assert request.json_schema is not None
    system = request.system or ""
    assert "Today is Wednesday 21 October 2026." in system
    assert "- reschedule_appointment: Move a booked visit" in system
    assert "format YYYY-MM-DDTHH:MM" in system


def test_fault_files_reach_the_prompt() -> None:
    settings = AgentSettings(brain="llm")
    clean = system_prompt(load_agent_files(), settings)
    faulty = system_prompt(files_for("F2"), settings)
    assert "MM/DD/YYYY" not in clean
    assert "MM/DD/YYYY" in faulty


def test_step_seeds_differ_by_position() -> None:
    seeds = {step_seed(0, turn, step) for turn in range(5) for step in range(5)}
    assert len(seeds) == 25
    assert all(0 <= seed < 2**31 - 1 for seed in seeds)


def test_the_llm_brain_needs_a_provider() -> None:
    with pytest.raises(AgentConfigError, match="needs an LLM provider"):
        AcmeAgent(load_agent_files(), AgentSettings(brain="llm"))


async def test_brain_is_usable_directly(world: World) -> None:
    llm = FakeLLM(script=[say(json.dumps({"x": 1}))])
    brain = LLMBrain(load_agent_files(), AgentSettings(model="fake/agent"), llm)
    assert await brain.reply("hi", ToolRunner(LocalWorldTools(world))) == '{"x": 1}'
