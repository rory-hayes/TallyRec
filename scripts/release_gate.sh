#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

: "${TEST_DATABASE_URL:?TEST_DATABASE_URL is required}"
: "${DATABASE_URL:=$TEST_DATABASE_URL}"

echo "[release-gate] applying migrations"
supabase migration up --db-url "$TEST_DATABASE_URL" --include-all

echo "[release-gate] running python test suite with coverage gate"
uv run pytest --cov=apps --cov=libs --cov-report=term-missing --cov-fail-under=95 tests/unit tests/db tests/integration tests/harness -q

echo "[release-gate] running deterministic golden verifier"
uv run python scripts/run_golden.py
uv run pytest tests/harness/test_determinism.py -q

echo "[release-gate] building web app"
cd "$ROOT_DIR/apps/web"
npm ci
npm run build
