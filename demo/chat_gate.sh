#!/usr/bin/env bash
# The 90-second demo, chat part (spec, "The 90-second demo"), with real runs and no model calls:
#
#   1. a copy of the reference agent (examples/acme-maintenance) and its clean baseline run;
#   2. ONE line of prompts/system.md changes ("book the first free slot"), the agent restarts;
#   3. `hecklr run` + `hecklr gate` on the scripted smoke suite: the gate's verdict, interval and
#      flipped scenarios, as the PR comment shows them.
#
# The agent runs on its deterministic rules brain, which reads the same prompt file, so the
# numbers are exactly repeatable and cost $0.
#
#   demo/chat_gate.sh            # everything; outputs in demo/out/chat/
#   demo/chat_gate.sh setup      # stop after the edit with the agent running (for demo.tape)
#   demo/chat_gate.sh teardown   # stop the agent that `setup` left running
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${DEMO_OUT:-$REPO/demo/out/chat}"
PROJECT="$OUT/acme"
PID_FILE="$OUT/agent.pid"
PROMPT_LINE="- Before booking or rescheduling, offer slots that match the customer's preferences and get their explicit confirmation of the exact slot."
EDITED_LINE="- To save the customer time, book or reschedule the first free slot find_slots returns as soon as the customer is verified; do not offer alternatives or ask the customer to confirm."

hecklr() { uv run --project "$REPO" hecklr "$@"; }

free_port() {
  uv run --project "$REPO" python -c \
    'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'
}

start_agent() {
  local port="$1"
  uv run --project "$REPO" python "$PROJECT/agent/serve.py" --brain rules --port "$port" \
    >"$OUT/agent.log" 2>&1 &
  echo $! >"$PID_FILE"
  for _ in $(seq 1 120); do
    if curl -sf "http://127.0.0.1:$port/healthz" >/dev/null; then return 0; fi
    sleep 0.5
  done
  echo "the agent did not come up; see $OUT/agent.log" >&2
  return 1
}

stop_agent() {
  if [[ -f "$PID_FILE" ]]; then
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    rm -f "$PID_FILE"
  fi
}

setup() {
  rm -rf "$OUT"
  mkdir -p "$OUT"
  rsync -a --exclude tests --exclude __pycache__ --exclude .hecklr --exclude PROGRESS.md \
    "$REPO/examples/acme-maintenance/" "$PROJECT/"
  local port
  port="$(free_port)"
  sed -i.bak "s#127.0.0.1:8000#127.0.0.1:$port#" "$PROJECT/hecklr.yaml" && rm "$PROJECT/hecklr.yaml.bak"
  cp "$PROJECT/prompts/system.md" "$OUT/system.md.orig"

  echo "== baseline: the clean agent"
  start_agent "$port"
  (cd "$PROJECT" && hecklr run --suite smoke --budget-usd 0) | tee "$OUT/baseline.txt"
  stop_agent

  echo "== the one-line prompt edit"
  uv run --project "$REPO" python - "$PROJECT/prompts/system.md" "$PROMPT_LINE" "$EDITED_LINE" <<'EOF'
import sys
from pathlib import Path

path, old, new = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = path.read_text(encoding="utf-8")
if text.count(old) != 1:
    raise SystemExit(f"expected the line exactly once in {path}")
path.write_text(text.replace(old, new), encoding="utf-8")
EOF
  (cd "$OUT" && diff -u system.md.orig acme/prompts/system.md || true) | tee "$OUT/edit.diff"
  start_agent "$port"
}

candidate() {
  echo "== candidate: the edited agent, then the gate"
  (cd "$PROJECT" && hecklr run --suite smoke --budget-usd 0) | tee "$OUT/candidate.txt"
  # The gate exits 1 on a regression, which is the point of the demo; pipefail carries its code.
  local status=0
  (cd "$PROJECT" && hecklr gate --suite smoke --out-dir "$OUT/gate") | tee "$OUT/gate.txt" ||
    status=$?
  echo "hecklr gate exit code: $status" | tee -a "$OUT/gate.txt"
}

case "${1:-all}" in
  setup) setup ;;
  teardown) stop_agent ;;
  all)
    trap stop_agent EXIT
    setup
    candidate
    ;;
  *) echo "usage: $0 [all|setup|teardown]" >&2; exit 64 ;;
esac
