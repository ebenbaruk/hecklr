"""SC1: the quickstart, executed literally, gives a first report in under 5 minutes.

The README and the docs site show the same three commands; this test reads them from both pages
and runs them in a fresh directory through the installed ``hecklr`` console script, as a user
would. ``pip install hecklr`` is the one step it does not run: the test environment is that
install (Hecklr is not on PyPI yet; see the quickstart's "From source" tab).

It reproduces the quickstart on a laptop with no model key and no local Ollama, where ``hecklr
init`` picks scripted ``fake/`` models: no network and no cost. Keys are removed from the
environment, and the proxy variables make ``localhost:11434`` unreachable on a developer machine
that runs Ollama (``hecklr init`` probes that fixed URL), while ``127.0.0.1`` (the world sandbox)
stays direct.
"""

import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest
from docs_snippets import DOCS, REPO, commands, marked_block

SC1_LIMIT_S = 300
_MODEL_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def _offline_laptop_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _MODEL_KEYS}
    env.update(
        HTTP_PROXY="http://127.0.0.1:9",
        http_proxy="http://127.0.0.1:9",
        NO_PROXY="127.0.0.1",
        no_proxy="127.0.0.1",
        NO_COLOR="1",
        COLUMNS="160",
    )
    return env


def _hecklr_argv(command: str) -> list[str]:
    argv = shlex.split(command)
    assert argv[0] == "hecklr", f"the quickstart runs hecklr commands only, got {command!r}"
    return [str(Path(sys.executable).with_name("hecklr")), *argv[1:]]


@pytest.mark.e2e
@pytest.mark.fake
@pytest.mark.slow
def test_sc1_quickstart_init_to_first_report_under_5_minutes(tmp_path: Path) -> None:
    readme = commands(marked_block(REPO / "README.md", "quickstart"))
    docs = commands(marked_block(DOCS / "quickstart.md", "quickstart"))
    assert readme == docs, "README and docs/quickstart.md must show the same quickstart"
    install, *steps = docs
    assert install == "pip install hecklr"
    assert len(steps) == 2, steps

    env = _offline_laptop_env()
    started = time.monotonic()
    outputs = []
    for step in steps:
        done = subprocess.run(  # noqa: S603 -- the documented command, run as a user would
            _hecklr_argv(step),
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=SC1_LIMIT_S,
            check=False,
        )
        assert done.returncode == 0, f"{step} exited {done.returncode}:\n{done.stdout}{done.stderr}"
        outputs.append(done.stdout)
    elapsed = time.monotonic() - started

    init_output, run_output = outputs
    assert "scripted fake models" in init_output  # the offline laptop the test stands for
    assert "Run metrics (95% CI)" in run_output
    assert "Scenarios (Wilson 95% CI)" in run_output
    report = re.search(r"Report: (\S+report\.html)", run_output)
    assert report, run_output
    html = Path(report.group(1)).read_text(encoding="utf-8")
    assert "<html" in html.lower()
    assert Path(report.group(1)).is_relative_to(tmp_path)
    assert elapsed < SC1_LIMIT_S, f"init to first report took {elapsed:.0f} s"
