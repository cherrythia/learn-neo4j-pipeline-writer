#!/usr/bin/env bash
# Run every check CI runs (except the docker demo job). Exits non-zero on the first failure.
#   ./scripts/check.sh          # backend + admin-ui
#   ./scripts/check.sh backend  # backend only
set -euo pipefail
cd "$(dirname "$0")/.."

step() { printf '\n== %s\n' "$*"; }

step "ruff check";        uv run ruff check .
step "ruff format";       uv run ruff format --check .
step "mypy";              uv run mypy
step "pytest";            uv run pytest

if [ "${1:-all}" != "backend" ]; then
  step "admin-ui typecheck"; (cd admin-ui && bun run typecheck)
  step "admin-ui test";      (cd admin-ui && bun test)
  step "admin-ui build";     (cd admin-ui && bun run build >/dev/null)
fi
printf '\nall checks passed\n'
