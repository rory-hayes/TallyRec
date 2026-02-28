from __future__ import annotations

import pytest
from psycopg import Connection

pytestmark = pytest.mark.usefixtures("clean_db")


REQUIRED_TABLES = [
    "firms",
    "firm_memberships",
    "clients",
    "runs",
    "source_files",
    "payroll_expected",
    "bank_transactions",
    "match_groups",
    "match_group_members",
    "variances",
    "audit_events",
    "jobs",
    "client_recon_policies",
    "run_bank_tieout_summaries",
]

REQUIRED_INDEXES = [
    "jobs_queue_idx",
    "runs_firm_client_status_idx",
    "variances_run_code_status_idx",
    "match_groups_run_idx",
]


def test_required_tables_and_indexes_exist(db_conn: Connection) -> None:
    with db_conn.cursor() as cur:
        for table in REQUIRED_TABLES:
            cur.execute("select to_regclass(%s) as rel", (f"public.{table}",))
            row = cur.fetchone()
            assert row and row["rel"] == f"public.{table}"

        for index in REQUIRED_INDEXES:
            cur.execute("select to_regclass(%s) as rel", (f"public.{index}",))
            row = cur.fetchone()
            assert row and row["rel"] == f"public.{index}"


def test_rls_enabled_for_tenant_tables(db_conn: Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            """
            select c.relname, c.relrowsecurity
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'public'
              and c.relname in ('firms', 'clients', 'runs', 'variances', 'match_groups')
            """
        )
        rows = cur.fetchall()
        assert rows
        assert all(row["relrowsecurity"] for row in rows)

        cur.execute(
            """
            select count(*) as total
            from pg_policies
            where schemaname = 'public'
            """
        )
        total = cur.fetchone()["total"]
        assert total >= 10
