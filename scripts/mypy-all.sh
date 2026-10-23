#!/usr/bin/env bash
# Type-check every workspace package plus the cross-package tests.
# Packages are checked one at a time because each has its own tests/ and conftest.py, which mypy
# would otherwise report as duplicate top-level modules.
set -euo pipefail
cd "$(dirname "$0")/.."
status=0
for package in packages/*/; do
  uv run --frozen mypy --strict "$package" || status=1
done
uv run --frozen mypy --strict conftest.py tests/test_network_guard.py || status=1
uv run --frozen mypy --strict tests/e2e || status=1
uv run --frozen mypy --strict tests/docs || status=1
uv run --frozen mypy --strict docs/scripts demo/voice_call.py || status=1
# Playwright is not a workspace dependency: the screenshot script declares it inline
# (`uv run --script`), so it is checked with that dependency layered on for this one call
# (`python -m mypy`: the layered interpreter, which sees it; the venv's mypy script does not).
uv run --frozen --with "playwright>=1.48" python -m mypy --strict demo/screenshots.py || status=1
exit "$status"
