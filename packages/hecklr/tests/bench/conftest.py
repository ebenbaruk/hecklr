"""A small benchmark directory: the real suite's chat track, cut to two scenarios.

Its one stack is the real acme-maintenance chat agent (rules brain) started as a process on a
free port, with Hecklr's scripted ``fake/`` customer and judge, so a whole ``hecklr bench run``
costs nothing and takes seconds.
"""

import shutil
import textwrap
from pathlib import Path
from typing import Final

import pytest

REPO: Final = Path(__file__).resolve().parents[4]
REAL_BENCH: Final = REPO / "bench"
ACME_SERVE: Final = REPO / "examples" / "acme-maintenance" / "agent" / "serve.py"
CHAT_SCENARIOS: Final = ("cancel-boiler-visit.yaml", "invoice-amount-due.yaml")

MANIFEST: Final = """\
version: "test-1"
seed: 7
suite: suite
world: worlds/acme-maintenance.yaml
models: {simulator: fake/customer, judge: fake/judge}
stats: {bootstrap_resamples: 500}
tracks:
  chat: {scenarios: "scenarios/chat/*.yaml", trials: 2}
"""

STACK: Final = f"""\
id: acme-rules
name: Acme chat, rules brain
track: chat
description: The reference chat agent's deterministic brain.
why: A free, exactly repeatable floor.
components: {{transport: HTTP SSE, brain: rules}}
command: ["{{python}}", "{ACME_SERVE}", "--brain", "rules", "--port", "{{port}}"]
env: {{ACME_SEED: "{{seed}}"}}
ready: {{http: "http://127.0.0.1:{{port}}/healthz", timeout_s: 60}}
adapter: {{type: http, url: "http://127.0.0.1:{{port}}/chat/stream", stream: sse}}
spend_url: "http://127.0.0.1:{{port}}/healthz"
budget_usd: 0.01
"""


@pytest.fixture
def bench_dir(tmp_path: Path) -> Path:
    """``bench.yaml``, a two-scenario chat suite and the ``acme-rules`` stack."""
    root = tmp_path / "bench"
    suite = root / "suite"
    shutil.copytree(REAL_BENCH / "suite" / "worlds", suite / "worlds")
    shutil.copytree(REAL_BENCH / "suite" / "personas", suite / "personas")
    (suite / "scenarios" / "chat").mkdir(parents=True)
    for name in CHAT_SCENARIOS:
        shutil.copy(
            REAL_BENCH / "suite" / "scenarios" / "chat" / name, suite / "scenarios" / "chat"
        )
    (root / "bench.yaml").write_text(MANIFEST, encoding="utf-8")
    (root / "stacks").mkdir()
    (root / "stacks" / "acme-rules.yaml").write_text(STACK, encoding="utf-8")
    shutil.copytree(REAL_BENCH / "site" / "src", root / "site" / "src")
    return root


def write_stack(bench_dir: Path, stack_id: str, body: str) -> None:
    """Add ``stacks/<stack_id>.yaml``."""
    path = bench_dir / "stacks" / f"{stack_id}.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
