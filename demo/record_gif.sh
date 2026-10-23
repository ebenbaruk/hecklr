#!/usr/bin/env bash
# Record docs/assets/demo.gif from a real run: the clean baseline and the one-line edit
# (chat_gate.sh setup), then demo.tape types the candidate run and the gate in a terminal.
# The project lives in a fresh temporary directory, so the paths the gate prints say nothing
# about this machine. Needs VHS (`brew install vhs`).
set -euo pipefail
cd "$(dirname "$0")/.."
DEMO_OUT="$(mktemp -d)/demo"
DEMO_VENV_BIN="$PWD/.venv/bin"
export DEMO_OUT DEMO_VENV_BIN
trap 'demo/chat_gate.sh teardown; rm -rf "$(dirname "$DEMO_OUT")"' EXIT
demo/chat_gate.sh setup
vhs demo/demo.tape
