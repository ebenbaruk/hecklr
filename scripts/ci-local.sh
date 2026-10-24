#!/usr/bin/env bash
# The merge gate (ADR 0022a): every check of .github/workflows/*.yml, run on this machine.
# Usage: scripts/ci-local.sh [--quick] [log-dir]   (default log dir: .ci-local/ in the repo)
#   --quick skips the slow steps (SC4 timing, perf, fault matrix, docs, UI, action self-test) for
#   iteration; a merge needs the full run.
# Exit 0 only if every step passed. Each step's output is in <log-dir>/<step>.log and the
# summary (with load averages, which SC4 timing depends on) in <log-dir>/summary.txt. A failed
# sc4-timing or perf step reruns its failed tests alone (<step>-rerun.log; see timing_step).
set -u
quick=0
if [ "${1:-}" = "--quick" ]; then quick=1; shift; fi
root=$(cd "$(dirname "$0")/.." && pwd) || exit 2
dir=${1:-$root/.ci-local}
out=$(mkdir -p "$dir" && cd "$dir" && pwd) || exit 2
cd "$root" || exit 2
# The gate never spends money: live tests are excluded by default and no key reaches them.
unset OPENAI_API_KEY ANTHROPIC_API_KEY ASSEMBLYAI_API_KEY
export UV_FROZEN=1 # as every workflow sets it: install the lockfile as committed, never re-lock

failed=()
rerun_passed=()
load() { uptime | sed 's/.*load averages*: //'; }
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$out/summary.txt"; }
run_step() { # name command... -> the command's exit status; output in <log-dir>/<name>.log
  local name=$1; shift
  log "START $name (load $(load))"
  "$@" > "$out/$name.log" 2>&1
  local rc=$?
  log "END   $name rc=$rc (load $(load))"
  return "$rc"
}
step() { run_step "$@" || failed+=("$1"); }

# A timing step (SC4 timing, perf) measures the machine as well as Hecklr. When it fails, its
# failed tests are rerun alone, once the 1-minute load is at most HECKLR_RERUN_MAX_LOAD (default:
# half the cores) or after HECKLR_RERUN_WAIT_S (default 900 s), and both results are reported.
# The step passes only if the rerun does (ADR 0024-hvoice-timing-under-load). Bounds never change.
rerun_max_load=${HECKLR_RERUN_MAX_LOAD:-$(( $(getconf _NPROCESSORS_ONLN) / 2 ))}
rerun_wait_s=${HECKLR_RERUN_WAIT_S:-900}
load_above() { uptime | sed 's/.*load averages*: //' | tr -d ',' | awk -v max="$1" '{exit !($1 > max)}'; }
timing_step() {
  local name=$1; shift
  run_step "$name" "$@" && return 0
  local ids=()
  # pytest's -ra summary names each failed test; colour codes (FORCE_COLOR) are stripped first.
  read -r -a ids <<< "$(sed -E $'s/\033\\[[0-9;]*m//g' "$out/$name.log" \
    | sed -nE 's/^(FAILED|ERROR) ([^ ]+).*/\2/p' | sort -u | tr '\n' ' ')"
  if [ "${#ids[@]}" = 0 ]; then
    log "RERUN $name: its log names no failed test, so there is nothing to rerun"
    failed+=("$name")
    return
  fi
  local waited=0
  while load_above "$rerun_max_load" && [ "$waited" -lt "$rerun_wait_s" ]; do
    sleep 30
    waited=$((waited + 30))
  done
  log "RERUN $name alone after ${waited} s (load limit $rerun_max_load): ${ids[*]}"
  if run_step "$name-rerun" "$@" --cov-append "${ids[@]}"; then
    rerun_passed+=("$name")
  else
    failed+=("$name")
  fi
}

: > "$out/summary.txt"
log "tree $(git rev-parse HEAD)$([ -z "$(git status --porcelain)" ] || echo ' + uncommitted changes')"
git merge-base --is-ancestor origin/main HEAD 2>/dev/null \
  || log "WARNING: origin/main is not merged into this tree (ADR 0022a gates the merged tree)"

# ci.yml: the serial SC4 and perf steps and the parallel step share one coverage file, as in CI.
step sync uv sync --all-packages --all-extras --group docs
step ruff uv run ruff check .
step format uv run ruff format --check .
step mypy scripts/mypy-all.sh
cov_append=()
if [ "$quick" = 0 ]; then
  timing_step sc4-timing env HECKLR_SHARED_CPU=1 uv run pytest -m sc4_timing -p no:xdist --cov --cov-report=
  timing_step perf uv run pytest -m perf -p no:xdist --cov --cov-append --cov-report=
  cov_append=(--cov-append)
fi
step tests uv run pytest -n auto --cov ${cov_append[@]+"${cov_append[@]}"} --cov-report=term --cov-fail-under=85
step e2e-fake uv run pytest tests/e2e -m fake

if [ "$quick" = 0 ]; then
  # fault-matrix.yml, free tier: clean A/A and F1-F6 on the rules brain, no model.
  rm -rf "$out/fault-matrix"
  step fault-matrix uv run python scripts/fault-matrix/fault_matrix.py --tier free --out "$out/fault-matrix"
  # docs.yml
  step mkdocs uv run mkdocs build --strict --site-dir "$out/site"
  step docs-tests uv run pytest tests/docs -p no:xdist
  # ui.yml (pins Node 24)
  node_major=$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null)
  [ "$node_major" = 24 ] || log "WARNING: ui.yml uses Node 24, found $(node --version 2>/dev/null || echo none)"
  step ui bash -c 'cd ui && npm ci && npm run lint && npm run typecheck && npm test && npm run build'
  # shellcheck disable=SC2016 # $1 expands in the inner shell, which gets the log dir as an argument
  step ui-wheel bash -c 'rm -rf "$1/dist" && uv build --package hecklr --wheel --out-dir "$1/dist" \
    && uv run --no-project python - "$1/dist" <<EOF
import glob, sys, zipfile
names = zipfile.ZipFile(glob.glob(sys.argv[1] + "/hecklr-*.whl")[0]).namelist()
assert "hecklr/ui_api/static/index.html" in names, "UI missing from the wheel"
assert any(n.startswith("hecklr/ui_api/static/assets/") for n in names), "UI assets missing from the wheel"
EOF' _ "$out"
  # hecklr-action-selftest.yml
  step action-selftest scripts/ci-selftest.sh "$out"
fi

if [ "${#rerun_passed[@]}" != 0 ]; then
  log "NOTE: ${rerun_passed[*]} failed under load and passed when rerun alone (both logs kept)"
fi
if [ "${#failed[@]}" = 0 ]; then
  log "PASS"
else
  log "FAIL: ${failed[*]} (logs in $out)"
  exit 1
fi
