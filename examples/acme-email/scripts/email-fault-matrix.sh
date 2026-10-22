#!/usr/bin/env bash
# The email fault matrix (SC8) through the real CLI: for the clean agent (twice, for A/A), F11 and
# F12, start the email agent behind its webhook, `hecklr run` a suite over the email channel,
# then `hecklr compare` each run with the first clean run.
#
#   examples/acme-email/scripts/email-fault-matrix.sh            # smoke suite, rules brain: $0
#   SUITE=ci BRAIN=llm BUDGET_USD=0.5 examples/acme-email/scripts/email-fault-matrix.sh
#
# Needs a `hecklr run` that drives the email channel (ADR 0012a, WS-wire). With luna in
# hecklr.yaml, the guided `ci` suite needs OPENAI_API_KEY in the environment; the scripted `smoke`
# suite calls no model. Reports land in examples/acme-email/.hecklr/matrix/.
set -euo pipefail
cd "$(dirname "$0")/../../.."

CONFIG=examples/acme-email/hecklr.yaml
RUNS=examples/acme-email/.hecklr/runs
REPORTS=examples/acme-email/.hecklr/matrix
SUITE=${SUITE:-smoke}
BRAIN=${BRAIN:-rules}
BUDGET_USD=${BUDGET_USD:-0}  # scripted smoke trials call no model and are estimated at $0
PORT=8100
LABELS=(clean clean-aa F11 F12)
RUN_IDS=()
AGENT_PID=""

stop_agent() {
  if [[ -n "$AGENT_PID" ]]; then
    kill "$AGENT_PID" 2>/dev/null || true
    wait "$AGENT_PID" 2>/dev/null || true
    AGENT_PID=""
  fi
}
trap stop_agent EXIT

start_agent() {
  local variant=()
  [[ "$1" == F* ]] && variant=(--fault "$1")
  uv run python examples/acme-email/agent/serve.py --brain "$BRAIN" ${variant[@]+"${variant[@]}"} \
    webhook --port "$PORT" >"$REPORTS/agent-$1.log" 2>&1 &
  AGENT_PID=$!
  for _ in $(seq 1 100); do
    curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && return 0
    sleep 0.2
  done
  echo "the $1 agent did not start; see $REPORTS/agent-$1.log" >&2
  return 1
}

# The id of the run that `hecklr run` printed in its log ("Run <ULID> (...)"), checked against the
# run directory it must have written: never an older run's, which would compare the wrong runs.
run_id_from() {
  local id
  id=$(grep -oE '^Run [0-9A-HJKMNP-TV-Z]{26} ' "$1" | tail -n 1 | cut -d' ' -f2 || true)
  if [[ -z "$id" || ! -d "$RUNS/$id" ]]; then
    echo "hecklr run wrote no run; see $1" >&2
    return 1
  fi
  echo "$id"
}

mkdir -p "$REPORTS"
for label in "${LABELS[@]}"; do
  echo "== $label: hecklr run --suite $SUITE --channels email ($BRAIN brain)"
  start_agent "$label"
  # A run with failing trials still records them; the comparison below is the verdict.
  uv run hecklr run -c "$CONFIG" -s "$SUITE" --channels email --budget-usd "$BUDGET_USD" \
    2>&1 | tee "$REPORTS/run-$label.log" || true
  curl -fsS "http://127.0.0.1:$PORT/healthz" >"$REPORTS/health-$label.json"
  stop_agent
  RUN_IDS+=("$(run_id_from "$REPORTS/run-$label.log")")
  echo "$label run: ${RUN_IDS[${#RUN_IDS[@]}-1]}"
done

for index in 1 2 3; do
  label=${LABELS[$index]}
  echo "== hecklr compare clean $label"
  uv run hecklr compare -c "$CONFIG" -f md --out "$REPORTS/compare-clean-$label.md" \
    "${RUN_IDS[0]}" "${RUN_IDS[$index]}" || true
  uv run hecklr compare -c "$CONFIG" "${RUN_IDS[0]}" "${RUN_IDS[$index]}" || true
done
echo "reports: $REPORTS"
