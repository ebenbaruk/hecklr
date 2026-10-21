"""A small on-disk project: config, world, scenarios and personas."""

import textwrap
from pathlib import Path

import pytest

from hecklr_core import Project, Suite, load_project, resolve_suite

CONFIG = """\
version: 1
agent:
  name: acme-support
  adapters:
    chat: { type: http, url: http://localhost:8000/chat }
    email: { type: smtp, inbox: support@acme.test }
world: worlds/acme.yaml
suites:
  ci: { scenarios: "scenarios/**/*.yaml", tags: [core], trials: 2, channels: [chat] }
  all: { trials: 1 }
  redteam: { personas: "personas/adversarial/*.yaml", trials: 1, channels: [chat] }
models:
  simulator: fake/sim
  judge: fake/judge
budget: { usd: 5, concurrency: 4 }
seed: 7
"""

WORLD = """\
id: acme
collections:
  appointments:
    key: customer_id
    records:
      - { customer_id: C-1042, slot: 2026-10-23T09:00 }
tools:
  - name: reschedule
    description: Move an appointment.
    op: { kind: update, collection: appointments }
"""

SCENARIOS = {
    "scenarios/reschedule.yaml": """\
id: reschedule
title: Move a visit
channels: [chat, email, voice]
tags: [core]
persona: [busy, calm]
world: acme
goal: Move the visit to next week
facts: { current_slot: 2026-10-23T09:00 }
success:
  - type: tool_called
    tool: reschedule
  - type: judge
    rubric: resolved
limits: { max_turns: 6, max_duration_s: 60 }
""",
    "scenarios/billing/invoice.yaml": """\
id: invoice
title: Ask about an invoice
channels: [chat]
tags: [billing]
persona: calm
world: acme
goal: Understand the invoice
success:
  - type: contains
    text: invoice
""",
}

PERSONAS = {
    "personas/busy.yaml": "id: busy\ndescription: Busy homeowner\n",
    "personas/calm.yaml": "id: calm\ndescription: Calm retiree\n",
    "personas/adversarial/fraud.yaml": (
        "id: fraud\ndescription: Wants a refund\nadversarial: refund_social_engineering\n"
    ),
}


def write_files(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    write_files(
        tmp_path,
        {"hecklr.yaml": CONFIG, "worlds/acme.yaml": WORLD, **SCENARIOS, **PERSONAS},
    )
    return tmp_path


@pytest.fixture
def project(project_dir: Path) -> Project:
    return load_project(project_dir / "hecklr.yaml")


@pytest.fixture
def ci_suite(project: Project) -> Suite:
    return resolve_suite(project, "ci")
