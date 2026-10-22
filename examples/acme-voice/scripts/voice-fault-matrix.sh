#!/usr/bin/env bash
# SC8 for voice through the real CLI: `hecklr run` + `hecklr compare` on the clean acme voice agent
# (twice: the A/A check) and on each voice fault F7-F10, against the first clean run.
#
#   examples/acme-voice/scripts/voice-fault-matrix.sh [timing|snr|all]
#
# Free: the agent runs the rules brain and local models (faster-whisper hears the callers; it
# speaks tagged synthetic audio that Hecklr's fake/tag ASR reads exactly), the callers speak with
# Kokoro, and the scripted suites call no LLM. Needs `uv sync --all-packages --all-extras`; the
# first run downloads the local models (~400 MB). Each agent run takes 3-5 minutes.
# Exit status: 0 when the clean agent shows no regression against itself and every fault is a
# regression, 1 otherwise.
set -euo pipefail
cd "$(dirname "$0")/../../.."

CONFIG=examples/acme-voice/hecklr.yaml
PORT=${HECKLR_VOICE_PORT:-8765}
MODELS=${HECKLR_VOICE_MODELS:-examples/acme-voice/.hecklr/models}
OUT=${HECKLR_MATRIX_OUT:-examples/acme-voice/.hecklr/matrix}
WHICH=${1:-all}
mkdir -p "$OUT"
agent_pid=""
failures=0

stop_agent() {
  if [[ -n "$agent_pid" ]]; then
    kill "$agent_pid" 2>/dev/null || true
    wait "$agent_pid" 2>/dev/null || true
    agent_pid=""
  fi
}
trap stop_agent EXIT

start_agent() { # variant (clean or F7..F10), suite, label
  local args=(--brain rules --tts synthetic --port "$PORT" --models-dir "$MODELS")
  [[ "$1" == clean ]] || args+=(--fault "$1")
  # The timing suite's callers are on clean lines: the energy VAD needs no model and no thread
  # hop per frame, so the agent's turn-taking does not wander with machine load. The SNR suites
  # need voice.yaml's noise-robust Silero.
  if [[ "$2" == voice-timing ]]; then
    args+=(--vad energy)
  fi
  uv run python examples/acme-voice/agent/serve.py "${args[@]}" >"$OUT/agent-$3.log" 2>&1 &
  agent_pid=$!
  for _ in $(seq 1 300); do
    curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null && return 0
    kill -0 "$agent_pid" 2>/dev/null || break
    sleep 1
  done
  echo "the $1 agent did not start; see $OUT/agent-$3.log" >&2
  exit 1
}

run_suite() { # variant suite label -> prints the run id
  start_agent "$1" "$2" "$3"
  uv run hecklr run --config "$CONFIG" --suite "$2" --budget-usd 0.5 >"$OUT/run-$3.log" 2>&1 || true
  stop_agent
  local run_id
  run_id=$(grep -Eo 'Run [0-9A-Z]{26}' "$OUT/run-$3.log" | tail -1 | cut -d' ' -f2)
  if [[ -z "$run_id" ]]; then
    echo "hecklr run recorded no run for $3; see $OUT/run-$3.log" >&2
    exit 1
  fi
  echo "$run_id"
}

verdict() { # baseline candidate label -> prints the suite verdict
  uv run hecklr compare --config "$CONFIG" "$1" "$2" --format json --out "$OUT/compare-$3.json" >/dev/null
  uv run hecklr compare --config "$CONFIG" "$1" "$2" >"$OUT/compare-$3.txt"
  uv run python -c 'import json, sys; print(json.load(open(sys.argv[1]))["verdict"])' \
    "$OUT/compare-$3.json"
}

expect() { # label verdict expected
  if [[ "$2" == "$3" || ("$3" == not-regression && "$2" != regression) ]]; then
    echo "  ok   $1: $2"
  else
    echo "  FAIL $1: $2 (expected $3)"
    failures=$((failures + 1))
  fi
}

matrix() { # suite faults...
  local suite=$1
  shift
  echo "== $suite"
  local baseline again
  baseline=$(run_suite clean "$suite" "$suite-clean-1")
  again=$(run_suite clean "$suite" "$suite-clean-2")
  expect "clean vs clean (A/A)" "$(verdict "$baseline" "$again" "$suite-aa")" not-regression
  for fault in "$@"; do
    local candidate
    candidate=$(run_suite "$fault" "$suite" "$suite-$fault")
    expect "$fault vs clean" "$(verdict "$baseline" "$candidate" "$suite-$fault")" regression
  done
}

[[ "$WHICH" == snr ]] || matrix voice-timing F7 F8 F9
[[ "$WHICH" == timing ]] || matrix voice-snr-10db F10
echo "reports and logs: $OUT"
exit $((failures > 0))
