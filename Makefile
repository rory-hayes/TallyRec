.PHONY: db-migrate test golden ci

DB_URL ?= postgresql://postgres:postgres@127.0.0.1:5432/postgres

db-migrate:
	supabase migration up --db-url "$(DB_URL)" --include-all

test:
	uv run pytest tests/unit tests/integration tests/db

golden:
	uv run pytest tests/harness

ci: db-migrate test golden
