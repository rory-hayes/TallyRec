.PHONY: db-migrate test golden ci

DB_URL ?= postgresql://postgres:postgres@127.0.0.1:5432/postgres

db-migrate:
	supabase migration up --db-url "$(DB_URL)" --include-all

test:
	uv run pytest --cov=apps --cov=libs --cov-report=term-missing --cov-fail-under=95 tests/unit tests/integration tests/db tests/harness

golden:
	uv run pytest tests/harness

ci: db-migrate test golden
