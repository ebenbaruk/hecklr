#!/usr/bin/env bash
# The merge gate (ADR 0022a): every check of .github/workflows/*.yml, run on this machine.
# Usage: scripts/ci-local.sh [--quick] [log-dir]   (default log dir: .ci-local/ in the repo)
#   --quick skips the slow steps (SC4 timing, perf, fault matrix, docs, UI, action self-test) for
#   iteration; a merge needs the full run.
# Exit 0 only if every step passed. Each step's output is in <log-dir>/<step>.log and the
# summary (with load averages, which SC4 timing depends on) in <log-dir>/summary.txt.
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
load() { uptime | sed 's/.*load averages*: //'; }
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$out/summary.txt"; }
step() {
  local name=$1; shift
  log "START $name (load $(load))"
  "$@" > "$out/$name.log" 2>&1
  local rc=$?
  log "END   $name rc=$rc (load $(load))"
  [ "$rc" = 0 ] || failed+=("$name")
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
  step sc4-timing env HECKLR_SHARED_CPU=1 uv run pytest -m sc4_timing -p no:xdist --cov --cov-report=
  step perf uv run pytest -m perf -p no:xdist --cov --cov-append --cov-report=
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

if [ "${#failed[@]}" = 0 ]; then
  log "PASS"
else
  log "FAIL: ${failed[*]} (logs in $out)"
  exit 1
fi
