# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright>=1.48"]
# ///
"""Screenshots of the demo's real reports and of the local UI, for the README and the docs.

Uses the Google Chrome already installed on the machine (Playwright's ``channel="chrome"``), so
no browser download is needed. Run after ``demo/chat_gate.sh`` and ``demo/voice_call.py``:

    uv run --script demo/screenshots.py                     # the HTML reports
    uv run --script demo/screenshots.py --ui http://127.0.0.1:8484   # and the running UI

Writes PNGs to ``docs/assets/``.
"""

import argparse
import json
from pathlib import Path

from playwright.sync_api import Page, ViewportSize, sync_playwright

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "docs" / "assets"
CHAT_GATE = REPO / "demo" / "out" / "chat" / "gate" / "report.html"
VOICE_RUNS = REPO / "demo" / "out" / "voice" / "acme-voice" / ".hecklr" / "runs"
VOICE_RUN_IDS = REPO / "demo" / "out" / "voice" / "runs.json"
VIEWPORT: ViewportSize = {"width": 1280, "height": 860}


def main() -> int:
    """Screenshot the gate report, a voice trial and, with --ui, the UI."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ui", help="base URL of a running `hecklr ui` over the voice demo")
    args = parser.parse_args()
    ASSETS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome")
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
        _gate_report(page)
        _voice_trial(page)
        if args.ui:
            _ui_trial(page, args.ui)
        browser.close()
    return 0


def _gate_report(page: Page) -> None:
    page.goto(CHAT_GATE.as_uri())
    page.screenshot(
        path=ASSETS / "report-gate.png",
        full_page=True,
        clip={"x": 0, "y": 0, "width": VIEWPORT["width"], "height": 1500},
    )


def _voice_trial(page: Page) -> None:
    runs = json.loads(VOICE_RUN_IDS.read_text(encoding="utf-8"))
    report = VOICE_RUNS / runs["clean"] / "report.html"
    page.goto(report.as_uri())
    trial = page.locator('details.trial[data-failed="no"]').first
    trial.locator("> summary").click()
    page.wait_for_timeout(1500)  # the waveform is drawn after the details element opens
    trial.screenshot(path=ASSETS / "report-voice-trial.png")


def _ui_trial(page: Page, base_url: str) -> None:
    # The footer shows the project's absolute path, which says nothing about Hecklr.
    page.add_init_script(
        "addEventListener('DOMContentLoaded', () => document.head.insertAdjacentHTML("
        "'beforeend', '<style>footer{display:none}</style>'))"
    )
    runs = json.loads(VOICE_RUN_IDS.read_text(encoding="utf-8"))
    clean, faulty = runs["clean"], runs["F8"]
    page.goto(f"{base_url}/compare?baseline={clean}&candidate={faulty}")
    page.wait_for_timeout(2000)
    page.screenshot(path=ASSETS / "ui-compare.png", full_page=True)
    trials = page.request.get(f"{base_url}/api/runs/{clean}").json()["trials"]
    trial = next(t["trial_id"] for t in trials if t["passed"])
    page.goto(f"{base_url}/runs/{clean}/trials/{trial}")
    page.wait_for_timeout(3000)  # wavesurfer draws once the recordings have loaded
    page.screenshot(path=ASSETS / "ui-trial.png", full_page=True)


if __name__ == "__main__":
    raise SystemExit(main())
