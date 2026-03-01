# TallyRec Sprint 4

Deterministic payroll reconciliation foundations with:

- PayrollExpected ↔ BankTransactions tie-out (Sprint 2)
- PayrollExpected ↔ GLJournal tie-out (Sprint 3)
- Bureau-first dashboard + batch runs + UK timing pack (Sprint 4)
- Variance resolution workflow + reviewer approval
- Immutable run locking on approval
- Deterministic audit pack export

## Stack

- Supabase SQL migrations (`supabase/migrations`)
- FastAPI backend (`apps/api`)
- Worker queue processor (`apps/worker`)
- Deterministic reconciliation engine (`libs/core/engine`)
- Golden regression harness (`tests/harness/scenarios` + `scripts/run_golden.py`)
- Next.js run UI + Variance Center (`apps/web`)

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
uv run pytest tests/harness/test_determinism.py -q
uv run pytest --cov=apps --cov=libs --cov-report=term-missing --cov-fail-under=95 tests/unit tests/db tests/integration tests/harness -q
```

```bash
uv run python scripts/run_golden.py
```

```bash
./scripts/release_gate.sh
```

```bash
cd apps/web
npm ci
npm run test:e2e
```

## Key API Endpoints

- `POST /v1/firms`
- `GET /v1/session`
- `GET /v1/firms`
- `GET /v1/firms/{firm_id}/clients`
- `POST /v1/clients`
- `POST /v1/runs`
- `GET /v1/runs?firm_id=...`
- `POST /v1/runs/{run_id}/source-files`
- `GET /v1/runs/{run_id}/source-files`
- `POST /v1/source-files/{source_file_id}/remap`
- `POST /v1/clients/{client_id}/mapping-templates`
- `GET /v1/clients/{client_id}/mapping-templates`
- `POST /v1/runs/{run_id}/jobs`
- `POST /v1/runs/{run_id}/reconcile/bank`
- `POST /v1/runs/{run_id}/reconcile/gl`
- `POST /v1/runs/{run_id}/export-pack`
- `GET /v1/runs/{run_id}/summary`
- `GET /v1/runs/{run_id}/bank-tieout`
- `GET /v1/runs/{run_id}/gl-tieout`
- `GET /v1/runs/{run_id}/variances?category=bank&status=open`
- `GET /v1/runs/{run_id}/variances/{variance_id}`
- `POST /v1/variances/{variance_id}/resolve`
- `POST /v1/variances/{variance_id}/approve-ignored`
- `GET /v1/runs/{run_id}/match-groups`
- `PATCH /v1/clients/{client_id}/recon-policy`
- `PATCH /v1/clients/{client_id}/uk-timing-policy`
- `PUT /v1/clients/{client_id}/bank-accounts`
- `PUT /v1/clients/{client_id}/gl-bucket-accounts`
- `POST /v1/batches/runs`
- `GET /v1/batches/{batch_id}`
- `GET /v1/dashboard`
- `GET /v1/ops/queue-stats`
- `POST /v1/runs/{run_id}/ready-for-review`
- `POST /v1/runs/{run_id}/approve`
- `POST /v1/runs/{run_id}/unlock`
- `GET /v1/runs/{run_id}/export-packs`
- `GET /v1/export-packs/{export_pack_id}/download`

All authenticated API calls expect header: `X-User-Id: <uuid>`.

## CI

GitHub Actions workflow: `.github/workflows/ci.yml`

- Applies migrations to a fresh Postgres database.
- Runs unit, DB, integration, harness, and determinism tests.
- Runs Playwright browser E2E against local API + Next.js web app.
