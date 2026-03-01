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
    "client_uk_timing_policies",
    "client_gl_bucket_accounts",
    "run_batches",
    "run_batch_items",
    "run_bank_tieout_summaries",
    "run_gl_tieout_summaries",
    "run_import_health_summaries",
    "variance_resolution_events",
]

REQUIRED_INDEXES = [
    "jobs_queue_idx",
    "runs_firm_client_status_idx",
    "variances_run_code_status_idx",
    "match_groups_run_idx",
    "client_gl_bucket_accounts_client_bucket_idx",
    "run_gl_tieout_summaries_status_idx",
    "variances_resolution_idx",
    "variance_resolution_events_variance_idx",
    "export_packs_run_hash_uidx",
    "runs_firm_due_status_idx",
    "run_batches_firm_status_created_idx",
    "run_batch_items_batch_status_idx",
    "run_import_health_summaries_firm_band_idx",
    "source_files_run_validation_idx",
]


def test_required_tables_and_indexes_exist(db_conn: Connection) -> None:
    with db_conn.cursor() as cur:
        for table in REQUIRED_TABLES:
            cur.execute("select to_regclass(%s) as rel", (f"public.{table}",))
            row = cur.fetchone()
            assert row and row["rel"] in {table, f"public.{table}"}

        for index in REQUIRED_INDEXES:
            cur.execute("select to_regclass(%s) as rel", (f"public.{index}",))
            row = cur.fetchone()
            assert row and row["rel"] in {index, f"public.{index}"}


def test_rls_enabled_for_tenant_tables(db_conn: Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            """
            select c.relname, c.relrowsecurity
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'public'
              and c.relname in (
                'firms', 'clients', 'runs', 'variances', 'match_groups',
                'run_gl_tieout_summaries', 'client_gl_bucket_accounts', 'variance_resolution_events',
                'run_batches', 'run_batch_items', 'client_uk_timing_policies', 'run_import_health_summaries'
              )
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
