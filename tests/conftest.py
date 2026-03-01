from __future__ import annotations

import os
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row

from apps.api.app.core.config import settings
from apps.api.app.main import app


PUBLIC_TABLES = [
    "run_batch_items",
    "run_batches",
    "variance_resolution_events",
    "run_gl_tieout_summaries",
    "run_bank_tieout_summaries",
    "run_import_health_summaries",
    "export_packs",
    "audit_events",
    "approvals",
    "variances",
    "match_group_members",
    "match_groups",
    "jobs",
    "gl_journal_lines",
    "bank_transactions",
    "payroll_expected",
    "source_files",
    "mapping_templates",
    "runs",
    "client_uk_timing_policies",
    "client_gl_bucket_accounts",
    "client_bank_accounts",
    "client_recon_policies",
    "clients",
    "firm_memberships",
    "firms",
]


def _resolve_db_url() -> str:
    return os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL") or ""


@pytest.fixture(scope="session")
def db_url() -> str:
    url = _resolve_db_url()
    if not url:
        pytest.skip("No TEST_DATABASE_URL or DATABASE_URL configured")
    return url


@pytest.fixture(scope="session")
def db_ready(db_url: str) -> None:
    try:
        with Connection.connect(db_url):
            pass
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Database unavailable: {exc}")


@pytest.fixture()
def db_conn(db_url: str, db_ready: None) -> Generator[Connection, None, None]:
    conn = Connection.connect(db_url, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def clean_db(db_conn: Connection) -> Generator[None, None, None]:
    with db_conn.cursor() as cur:
        cur.execute("truncate table auth.users cascade")
        cur.execute(
            "truncate table " + ", ".join(f"public.{name}" for name in PUBLIC_TABLES) + " restart identity cascade"
        )
    db_conn.commit()
    yield
    with db_conn.cursor() as cur:
        cur.execute("truncate table auth.users cascade")
        cur.execute(
            "truncate table " + ", ".join(f"public.{name}" for name in PUBLIC_TABLES) + " restart identity cascade"
        )
    db_conn.commit()


@pytest.fixture()
def api_client(db_url: str, db_ready: None) -> Generator[TestClient, None, None]:
    settings.database_url = db_url
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()
