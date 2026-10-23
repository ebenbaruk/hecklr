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
# The reference agents are not workspace packages: their import roots go on MYPYPATH (#67).
MYPYPATH=examples/acme-maintenance/agent:examples/acme-email/agent:examples/acme-voice/agent \
  uv run --frozen mypy --strict -p acme_agent -p acme_email -p acme_voice || status=1
exit "$status"
