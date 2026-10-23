#!/usr/bin/env bash
# Local mirror of .github/workflows/hecklr-action-selftest.yml: the composite action's
# run -> latest-run -> gate steps on the reference agent (rules brain, scripted customers, no
# model). A clean candidate must pass (0), seeded fault F3 must regress (1), and an agent that
# cannot start must be a harness error (3) that records no run. The GitHub-only steps (cache,
# artifact upload, PR comment) have no local equivalent. Two differences from a fresh job: it
# reuses the current environment (the action syncs without extras), and earlier local runs stay
# in examples/acme-maintenance/.hecklr (harmless: `latest-run --since` picks only this run's).
# Usage: scripts/ci-selftest.sh <log-dir>
set -u
root=$(cd "$(dirname "$0")/.." && pwd) || exit 2
out=$(cd "${1:?usage: ci-selftest.sh <log-dir>}" && pwd) || exit 2
project=$root/examples/acme-maintenance
cd "$project" || exit 2
unset OPENAI_API_KEY

cp "$root/action/selftest/hecklr.yaml" hecklr.action.yaml
cp "$root/action/selftest/hecklr_action_agent.py" .
sed 's#hecklr_action_agent:reply#hecklr_action_agent:no_such_agent#' \
  "$root/action/selftest/hecklr.yaml" > hecklr.unusable.yaml
cleanup() { rm -rf hecklr.action.yaml hecklr_action_agent.py hecklr.unusable.yaml hecklr-gate.md hecklr-report; }
trap cleanup EXIT

# action <config> <suite> <baseline-run-or-empty>: prints "<gate exit> <candidate run id>"
action() {
  local config=$1 suite=$2 baseline=$3 ids since run_exit candidate code
  rm -rf hecklr-report hecklr-gate.md # as action.yml does before each run
  since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  sleep 1
  local args=(run --config "$config" --suite "$suite")
  [ -n "$baseline" ] && args+=(--baseline "$baseline")
  uv run hecklr "${args[@]}" > "$out/selftest-$suite-$config.run.log" 2>&1
  run_exit=$?
  ids=$(mktemp)
  GITHUB_OUTPUT=$ids uv run hecklr github latest-run --config "$config" --suite "$suite" \
    --since "$since" > /dev/null 2>&1 || true
  candidate=$(sed -n 's/^run-id=//p' "$ids" | tail -1)
  rm -f "$ids"
  if [ -z "$candidate" ]; then
    code=3
  else
    local gate=(gate --config "$config" --suite "$suite" --candidate "$candidate"
      --out-dir hecklr-report --missing-baseline pass)
    [ -n "$baseline" ] && gate+=(--baseline "$baseline")
    uv run hecklr "${gate[@]}" > "$out/selftest-$suite-$config.gate.md" 2>&1
    code=$?
    # The action fails the job on a run that errored even if the gate passed.
    if [ "$run_exit" != 0 ] && [ "$code" != 3 ]; then code=3; fi
  fi
  echo "$code $candidate"
}

read -r baseline baseline_run < <(action hecklr.action.yaml smoke-baseline "")
read -r clean _ < <(action hecklr.action.yaml smoke "$baseline_run")
read -r fault _ < <(ACME_FAULT=F3 action hecklr.action.yaml smoke-fault "$baseline_run")
read -r unusable unusable_run < <(action hecklr.unusable.yaml smoke "$baseline_run")
echo "baseline=$baseline clean=$clean fault=$fault unusable=$unusable unusable-run=${unusable_run:-none}"
[ "$baseline" = 0 ] && [ "$clean" = 0 ] && [ "$fault" = 1 ] && [ "$unusable" = 3 ] && [ -z "${unusable_run:-}" ]
