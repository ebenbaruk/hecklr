#!/usr/bin/env bash
# The merge gate (ADR 0022a): every check of .github/workflows/*.yml, run on this machine.
# Usage: scripts/ci-local.sh [--quick] [log-dir]   (default log dir: .ci-local/)
#   --quick skips the slow steps (SC4 timing, fault matrix, UI, docs) for iteration; a merge
#   needs the full run.
# Exit 0 only if every step passed. Each step's output is in <log-dir>/<step>.log and the
# summary (with load averages, which SC4 timing depends on) in <log-dir>/summary.txt.
set -u
cd "$(dirname "$0")/.." || exit 2
quick=0
if [ "${1:-}" = "--quick" ]; then quick=1; shift; fi
out=$(mkdir -p "${1:-.ci-local}" && cd "${1:-.ci-local}" && pwd)
# The gate never spends money: live tests are excluded by default and no key reaches them.
unset OPENAI_API_KEY ANTHROPIC_API_KEY ASSEMBLYAI_API_KEY

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
log "tree $(git rev-parse HEAD)$(git diff --quiet HEAD || echo ' + uncommitted changes')"

# ci.yml
step sync uv sync --all-packages --all-extras --group docs
step ruff uv run ruff check .
step format uv run ruff format --check .
step mypy scripts/mypy-all.sh
if [ "$quick" = 0 ]; then
  step sc4-timing env HECKLR_SHARED_CPU=1 uv run pytest -m sc4_timing -p no:xdist
fi
step tests uv run pytest -n auto --cov --cov-report=term --cov-fail-under=85
step e2e-fake uv run pytest tests/e2e -m fake

if [ "$quick" = 0 ]; then
  # fault-matrix.yml, free tier: clean A/A and F1-F6 on the rules brain, no model.
  rm -rf "$out/fault-matrix"
  step fault-matrix uv run python scripts/fault-matrix/fault_matrix.py --tier free --out "$out/fault-matrix"
  # docs.yml
  step mkdocs uv run mkdocs build --strict --site-dir "$out/site"
  step docs-tests uv run pytest tests/docs -p no:xdist
  # ui.yml
  step ui bash -c 'cd ui && npm ci && npm run lint && npm run typecheck && npm test && npm run build'
  step ui-wheel bash -c "rm -rf '$out/dist' && uv build --package hecklr --wheel --out-dir '$out/dist' \
    && uv run --no-project python -c \"
import glob, zipfile
names = zipfile.ZipFile(glob.glob('$out/dist/hecklr-*.whl')[0]).namelist()
assert 'hecklr/ui_api/static/index.html' in names, 'UI missing from the wheel'
\""
  # hecklr-action-selftest.yml
  step action-selftest scripts/ci-selftest.sh "$out"
fi

if [ "${#failed[@]}" = 0 ]; then
  log "PASS"
else
  log "FAIL: ${failed[*]} (logs in $out)"
  exit 1
fi
