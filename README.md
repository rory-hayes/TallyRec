# TallyRec Sprint 2

Deterministic PayrollExpected ↔ BankTransactions reconciliation v1.

## Stack

- Supabase SQL migrations (`supabase/migrations`)
- FastAPI backend (`apps/api`)
- Worker queue processor (`apps/worker`)
- Deterministic reconciliation engine (`libs/core/engine`)
- Golden regression harness (`tests/harness/scenarios` + `scripts/run_golden.py`)
- Next.js read-only run UI (`apps/web`)

## Local Commands

```bash
uv sync --extra dev
```

```bash
supabase migration up --db-url "$TEST_DATABASE_URL" --include-all
```

```bash
uv run uvicorn apps.api.app.main:app --reload
```

```bash
uv run python -m apps.worker.main --once
```

```bash
uv run pytest tests/unit -q
uv run pytest tests/harness -q
uv run pytest tests/db -q
uv run pytest tests/integration -q
```

```bash
uv run python scripts/run_golden.py
```

## Key API Endpoints

- `POST /v1/firms`
- `POST /v1/clients`
- `POST /v1/runs`
- `POST /v1/runs/{run_id}/source-files`
- `POST /v1/runs/{run_id}/jobs`
- `POST /v1/runs/{run_id}/reconcile/bank`
- `GET /v1/runs/{run_id}/summary`
- `GET /v1/runs/{run_id}/bank-tieout`
- `GET /v1/runs/{run_id}/variances?category=bank&status=open`
- `GET /v1/runs/{run_id}/match-groups`
- `PATCH /v1/clients/{client_id}/recon-policy`
- `PUT /v1/clients/{client_id}/bank-accounts`

All authenticated API calls expect header: `X-User-Id: <uuid>`.

## CI

GitHub Actions workflow: `.github/workflows/ci.yml`

- Applies migrations to a fresh Postgres database.
- Runs unit, DB, integration, harness, and determinism tests.
