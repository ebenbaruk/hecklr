"""Issue #76: the pre-run cost estimate is an upper bound on what a judged suite really spends.

The reproduction is the issue's run: the ``matrix`` suite (33 scripted scenarios plus 18
judge-only twins, 51 trials) against the rules brain with the F1 prompt edit, judges on
``openai/gpt-6-luna``. The judge is a ``FakeLLM`` billed at luna's price per token, with tokens
counted at the density the issue's real run was billed at, so the fake run costs what luna did
($0.0183) without a network call.
"""

import json
import math
import sys
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Final, cast

import pytest
import structlog
import yaml
from acme_agent import PROJECT_ROOT

from hecklr.compose import RunRequest, execute_run, prepare_run
from hecklr_core import RunResults
from hecklr_core.planning import JUDGE_ASKS_PER_VOTE, JUDGE_INSTRUCTIONS_CHARS, WORLD_BLIND_RUBRICS
from hecklr_grade.judge import (
    BUILTIN_RUBRICS,
    INTERNAL_RUBRICS,
    JUDGE_RETRIES,
    JUDGE_RETRY,
    JUDGE_SYSTEM,
    JudgeVerdict,
    load_rubric,
    render_prompt,
)
from hecklr_llm import FakeLLM, FakeReply, Responder, cost_usd
from hecklr_schemas import GradeContext, LLMRequest, LLMResponse, LLMUsage, RunStatus

ISSUE_SPEND_USD: Final = Decimal("0.0183")
"""What the issue's run billed on gpt-6-luna for its 18 judge calls."""
LUNA_CHARS_PER_TOKEN: Final = 2.6
"""Prompt characters per billed token that reproduce ``ISSUE_SPEND_USD``: judge prompts are
mostly world JSON and ULIDs, which tokenize densely."""
AGENT_MODULE: Final = "acme_cost_agent"
AGENT_SOURCE: Final = """\
from acme_agent import (
    PROJECT_ROOT, WORLD_FILE, AcmeAgent, AgentSettings, InProcessAgent, fault_overlay,
    load_agent_files,
)
from hecklr_world import load_world

reply = InProcessAgent(
    AcmeAgent(load_agent_files(overlay=fault_overlay("F1")), AgentSettings(brain="rules")),
    load_world(PROJECT_ROOT / WORLD_FILE),
)
"""


class LunaPricedJudge:
    """A ``FakeLLM`` judge billed as gpt-6-luna bills the same prompt."""

    def __init__(self, responder: Responder) -> None:
        self.fake = FakeLLM(responder=responder)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        response = await self.fake.complete(request)
        chars = sum(len(part) for part in _prompt_parts(request))
        usage = LLMUsage(
            input_tokens=math.ceil(chars / LUNA_CHARS_PER_TOKEN),
            output_tokens=response.usage.output_tokens,
        )
        return response.model_copy(
            update={"usage": usage, "cost_usd": cost_usd(request.model, usage)}
        )


def _prompt_parts(request: LLMRequest) -> Iterator[str]:
    yield request.system or ""
    yield from (message.content for message in request.messages)
    yield json.dumps(request.json_schema)


def passing(request: LLMRequest) -> FakeReply:
    return {"verdict": "pass", "rationale": "Scripted verdict.", "evidence": []}


def unusable_first(request: LLMRequest) -> FakeReply:
    """Each vote's first answer cites an event that does not exist, so it is asked again (#37)."""
    if len(request.messages) > 1:
        return passing(request)
    return {"verdict": "pass", "rationale": "Scripted verdict.", "evidence": ["no-such-event"]}


@pytest.fixture
def matrix_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The acme project with the F1 rules brain behind the in-process ``python`` adapter."""
    config = yaml.safe_load((PROJECT_ROOT / "hecklr.yaml").read_text("utf-8"))
    config["agent"]["adapters"]["chat"] = {"type": "python", "entrypoint": f"{AGENT_MODULE}:reply"}
    config["output_dir"] = str(tmp_path / "out")
    project = tmp_path / "acme"
    project.mkdir()
    for name in ("worlds", "scripted", "personas"):
        (project / name).symlink_to(PROJECT_ROOT / name)
    (project / "hecklr.yaml").write_text(yaml.safe_dump(config), "utf-8")
    (project / f"{AGENT_MODULE}.py").write_text(AGENT_SOURCE, "utf-8")
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, AGENT_MODULE, raising=False)
    return project / "hecklr.yaml"


async def run_matrix(config: Path, judge: LunaPricedJudge) -> tuple[Decimal, RunResults]:
    """The matrix suite with its budget set to its own estimate: (estimate, results)."""
    estimate = prepare_run(RunRequest(config_path=config, suite="matrix")).plan.estimate.total_usd
    prepared = prepare_run(RunRequest(config_path=config, suite="matrix", budget_usd=estimate))
    logging_config = structlog.get_config()  # the run configures logging for its process
    try:
        results = await execute_run(prepared, llm_providers={"openai": judge})
    finally:
        structlog.configure(**logging_config)
        await sys.modules[AGENT_MODULE].reply.aclose()
    return estimate, results


@pytest.mark.anyio
@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
async def test_issue76_a_budget_set_from_the_estimate_finishes_the_judged_matrix(
    matrix_config: Path,
) -> None:
    judge = LunaPricedJudge(passing)
    estimate, results = await run_matrix(matrix_config, judge)

    assert len(results.trials) == 51
    assert len(judge.fake.calls) == 18
    # The fake run bills what the issue's luna run billed, give or take 10%...
    assert abs(results.run.spent_usd - ISSUE_SPEND_USD) <= ISSUE_SPEND_USD / 10
    # ... and the run the issue's estimate would have stopped partway finishes within it.
    assert results.run.status is RunStatus.COMPLETED
    assert results.run.spent_usd <= estimate


@pytest.mark.anyio
@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
async def test_issue76_the_estimate_covers_a_retry_of_every_judge_vote(
    matrix_config: Path,
) -> None:
    judge = LunaPricedJudge(unusable_first)
    estimate, results = await run_matrix(matrix_config, judge)

    assert len(judge.fake.calls) == 18 * JUDGE_ASKS_PER_VOTE
    assert results.run.status is RunStatus.COMPLETED
    assert results.run.spent_usd <= estimate


def test_issue76_the_estimates_judge_constants_match_hecklr_grade() -> None:
    """The planner sizes judge prompts without importing hecklr-grade; this keeps them in step."""
    assert JUDGE_RETRIES + 1 == JUDGE_ASKS_PER_VOTE
    rubrics = [load_rubric(name) for name in (*BUILTIN_RUBRICS, *INTERNAL_RUBRICS)]
    blind = {rubric.name for rubric in rubrics if not rubric.include_world}
    # The planner charges world snapshots to every rubric it does not know to be world-blind.
    assert blind >= WORLD_BLIND_RUBRICS
    assert not load_rubric("injection_resisted").include_world
    schema = json.dumps(JudgeVerdict.model_json_schema())
    for rubric in rubrics:
        empty = SimpleNamespace(
            scenario=SimpleNamespace(goal="", facts={}),
            trial=SimpleNamespace(
                world_before=SimpleNamespace(collections={}),
                world_after=SimpleNamespace(collections={}),
            ),
            events=[],
        )
        prompt = render_prompt(rubric, cast(GradeContext, empty))
        fixed = len(JUDGE_SYSTEM) + len(prompt) + len(schema) + len(JUDGE_RETRY)
        assert fixed <= JUDGE_INSTRUCTIONS_CHARS, rubric.name
