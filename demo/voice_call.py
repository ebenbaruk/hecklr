"""The 90-second demo, voice part: a caller on a noisy street line interrupts the agent.

Real local calls, no model spend: the reference voice agent (``examples/acme-voice``, rules
brain, Kokoro voice, Silero VAD, faster-whisper ears) is called by Hecklr's simulated callers, who
speak with Kokoro over street noise at 15 dB SNR on a G.711 phone line and barge in on every agent
turn. The script runs the clean agent, then fault F8 (barge-in disabled: one line of
``voice.yaml``), and compares the two, so the report shows the interruption on the waveform and
the comparison shows what the fault does to barge-in reaction time.

    uv run python demo/voice_call.py            # outputs in demo/out/voice/
    uv run python demo/voice_call.py --variants clean

Local models (Kokoro, faster-whisper, Silero) download on first use and are kept in
``demo/out/voice`` for the next run.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx2
import yaml

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "demo" / "out" / "voice"
SUITE = "demo-noisy-barge-in"
PERSONA = {
    "id": "noisy-interrupter",
    "description": "Calls from a busy street on a cheap phone line and cuts the agent off.",
    "language": "en-US",
    "traits": {"patience": 0.2, "verbosity": 0.3, "cooperativeness": 0.6},
    "behaviors": {"interrupt_prob": 1.0, "response_gap_ms": [200, 400]},
    "voice": {
        "tts": {"provider": "kokoro", "voice": "am_michael", "rate": 1.1},
        "environment": "street",
        "snr_db": 15,
        "line": "g711_mulaw_8k",
    },
}
_IGNORED = shutil.ignore_patterns("tests", "__pycache__", ".hecklr", "PROGRESS.md")


def main() -> int:
    """Run the requested variants and compare the last one with the first."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--variants", default="clean,F8", help="comma-separated: clean, F7..F10")
    args = parser.parse_args()
    variants = args.variants.split(",")

    project = _prepare()
    runs: dict[str, str] = {}
    for variant in variants:
        port = _free_port()
        _set_port(project / "hecklr.yaml", port)
        with AgentProcess(project, port, variant):
            runs[variant] = _run(project, variant)
        (OUT / "runs.json").write_text(json.dumps(runs, indent=2), encoding="utf-8")
    if len(runs) > 1:
        first, last = runs[variants[0]], runs[variants[-1]]
        _hecklr(project, "compare", first, last, "-f", "md", "--out", str(OUT / "compare.md"))
        sys.stdout.write((OUT / "compare.md").read_text(encoding="utf-8"))
    return 0


def _prepare() -> Path:
    # Copied over any earlier copy: its .hecklr/ keeps the downloaded caller models and old runs.
    for name in ("acme-voice", "acme-maintenance", "acme-faults"):
        shutil.copytree(REPO / "examples" / name, OUT / name, ignore=_IGNORED, dirs_exist_ok=True)
    project = OUT / "acme-voice"
    personas = project / "personas" / "demo"
    personas.mkdir(exist_ok=True)
    (personas / "noisy-interrupter.yaml").write_text(yaml.safe_dump(PERSONA), encoding="utf-8")
    config_path = project / "hecklr.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["suites"][SUITE] = {
        "scenarios": "scenarios/timing/barge-in-*.yaml",
        "personas": "personas/demo/*.yaml",
        "trials": 1,
        "channels": ["voice"],
    }
    # The agent speaks with Kokoro here, so the caller hears it with a real recognizer.
    config["models"]["asr"] = "faster-whisper/base.en"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return project


def _set_port(config_path: Path, port: int) -> None:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["agent"]["adapters"]["voice"]["url"] = f"ws://127.0.0.1:{port}/"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


class AgentProcess:
    """The reference voice agent in its own process, for one variant."""

    def __init__(self, project: Path, port: int, variant: str) -> None:
        self._command = [
            sys.executable,
            str(project / "agent" / "serve.py"),
            "--brain",
            "rules",
            "--tts",
            "kokoro",
            "--port",
            str(port),
            "--models-dir",
            str(OUT / "agent-models"),
        ]
        if variant != "clean":
            self._command += ["--fault", variant]
        self._port = port
        self._log = OUT / f"agent-{variant}.log"
        self._process: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> None:
        with self._log.open("wb") as log:
            self._process = subprocess.Popen(self._command, stdout=log, stderr=subprocess.STDOUT)  # noqa: S603 -- our own agent
        deadline = time.monotonic() + 600  # the first start downloads the local models
        while time.monotonic() < deadline:
            try:
                if httpx2.get(f"http://127.0.0.1:{self._port}/healthz", timeout=2).is_success:
                    return
            except httpx2.HTTPError:
                # Not listening yet; poll again.
                pass
            if self._process.poll() is not None:
                raise SystemExit(f"the agent exited; see {self._log}")
            time.sleep(1)
        raise SystemExit(f"the agent did not come up in 10 minutes; see {self._log}")

    def __exit__(self, *_: object) -> None:
        if self._process is not None:
            self._process.terminate()
            self._process.wait(timeout=30)


def _run(project: Path, variant: str) -> str:
    output = _hecklr(project, "run", "--suite", SUITE, "--budget-usd", "0")
    (OUT / f"run-{variant}.txt").write_text(output, encoding="utf-8")
    sys.stdout.write(output)
    for line in output.splitlines():
        if line.startswith("Run ") and line.endswith("(completed)"):
            return line.split()[1]
    raise SystemExit(f"no completed run for {variant}; see {OUT / f'run-{variant}.txt'}")


def _hecklr(project: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603 -- the hecklr CLI of this environment
        [str(Path(sys.executable).with_name("hecklr")), *args],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "COLUMNS": "120", "NO_COLOR": "1"},
    )
    if done.returncode not in (0, 1, 2):  # compare/gate verdicts are not failures here
        raise SystemExit(f"hecklr {' '.join(args)} exited {done.returncode}:\n{done.stderr}")
    return done.stdout


if __name__ == "__main__":
    raise SystemExit(main())
