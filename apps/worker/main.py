from __future__ import annotations

import argparse
import json
import os
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row

from libs.core.engine import (
    BankTransaction,
    GLJournalLine,
    PayrollExpected,
    ReconPolicy,
    UKTimingPolicy,
    add_business_days,
    build_timing_variances,
    reconcile_bank,
    reconcile_gl,
)
from libs.core.utils import build_export_pack, compute_import_health_score, get_storage_client

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/postgres")


def _connect() -> Connection:
    return Connection.connect(DATABASE_URL, row_factory=dict_row)


def _audit(
    conn: Connection,
    *,
    firm_id: str | None,
    client_id: str | None,
    run_id: str | None,
    event_type: str,
    actor_user_id: str | None,
    entity_type: str,
    entity_id: str,
    payload: dict[str, Any] | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into public.audit_events(
              firm_id, client_id, run_id, event_type, actor_user_id,
              entity_type, entity_id, payload
            )
            values (%s, %s, %s, %s, %s::uuid, %s, %s::uuid, %s::jsonb)
            """,
            (
                firm_id,
                client_id,
                run_id,
                event_type,
                actor_user_id,
                entity_type,
                entity_id,
                json.dumps(payload or {}, sort_keys=True),
            ),
        )


def claim_next_job(conn: Connection) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            with next_job as (
              select id
              from public.jobs
              where status = 'queued'
              order by queued_at, id
              for update skip locked
              limit 1
            )
            update public.jobs j
            set status = 'running',
                started_at = now(),
                attempt_count = attempt_count + 1
            from next_job
            where j.id = next_job.id
            returning j.*
            """
        )
        return cur.fetchone()


def _load_policy(conn: Connection, client_id: str) -> tuple[ReconPolicy, set[str]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select amount_tolerance, date_window_days, max_group_size,
                   enable_one_to_many, enable_many_to_one, require_allowed_account
            from public.client_recon_policies
            where client_id = %s
            """,
            (client_id,),
        )
        policy_row = cur.fetchone()
        if policy_row:
            policy = ReconPolicy(
                amount_tolerance=Decimal(str(policy_row["amount_tolerance"])),
                date_window_days=int(policy_row["date_window_days"]),
                max_group_size=int(policy_row["max_group_size"]),
                enable_one_to_many=bool(policy_row["enable_one_to_many"]),
                enable_many_to_one=bool(policy_row["enable_many_to_one"]),
                require_allowed_account=bool(policy_row["require_allowed_account"]),
            )
        else:
            policy = ReconPolicy()

        cur.execute(
            """
            select account_ref
            from public.client_bank_accounts
            where client_id = %s
            """,
            (client_id,),
        )
        allowed_accounts = {row["account_ref"] for row in cur.fetchall()}
    return policy, allowed_accounts


def _load_run_meta(conn: Connection, run_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, firm_id, client_id, period_start, period_end
            from public.runs
            where id = %s
            """,
            (run_id,),
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError("Run not found")
        return dict(row)


def _load_uk_timing_policy(conn: Connection, client_id: str) -> UKTimingPolicy:
    with conn.cursor() as cur:
        cur.execute(
            """
            select tax_due_day, pension_due_day, bacs_visibility_business_days, holiday_calendar, enabled
            from public.client_uk_timing_policies
            where client_id = %s
            """,
            (client_id,),
        )
        row = cur.fetchone()
        if not row:
            return UKTimingPolicy()
        return UKTimingPolicy(
            tax_due_day=int(row["tax_due_day"]),
            pension_due_day=int(row["pension_due_day"]),
            bacs_visibility_business_days=int(row["bacs_visibility_business_days"]),
            holiday_calendar=row["holiday_calendar"],
            enabled=bool(row["enabled"]),
        )


def _parse_as_of_date(job_payload: dict[str, Any]) -> date:
    raw = job_payload.get("as_of_date")
    if not raw:
        return datetime.now(timezone.utc).date()
    if isinstance(raw, date):
        return raw
    return date.fromisoformat(str(raw))


def _sla_state(as_of_date: date, must_close_by_date: date | None) -> str | None:
    if must_close_by_date is None:
        return None
    if as_of_date > must_close_by_date:
        return "overdue"
    due_soon_cutoff = add_business_days(as_of_date, 2, "GB")
    if must_close_by_date <= due_soon_cutoff:
        return "due_soon"
    return "on_track"


def _load_payroll_expected(conn: Connection, run_id: str) -> list[PayrollExpected]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, payment_date, net_amount, employee_ref,
                   tax_amount, pension_amount, other_amount
            from public.payroll_expected
            where run_id = %s
            order by id
            """,
            (run_id,),
        )
        rows = cur.fetchall()
    return [
        PayrollExpected(
            id=str(row["id"]),
            payment_date=row["payment_date"],
            net_amount=Decimal(str(row["net_amount"])),
            employee_ref=row.get("employee_ref"),
            tax_amount=Decimal(str(row.get("tax_amount", "0"))),
            pension_amount=Decimal(str(row.get("pension_amount", "0"))),
            other_amount=Decimal(str(row.get("other_amount", "0"))),
        )
        for row in rows
    ]


def _load_bank_transactions(conn: Connection, run_id: str) -> list[BankTransaction]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, posted_date, amount_signed, account_ref, description
            from public.bank_transactions
            where run_id = %s
            order by id
            """,
            (run_id,),
        )
        rows = cur.fetchall()
    return [
        BankTransaction(
            id=str(row["id"]),
            posted_date=row["posted_date"],
            amount_signed=Decimal(str(row["amount_signed"])),
            account_ref=row.get("account_ref"),
            description=row.get("description"),
        )
        for row in rows
    ]


def _load_gl_journal_lines(conn: Connection, run_id: str) -> list[GLJournalLine]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, entry_date, account_code, debit_amount, credit_amount, description
            from public.gl_journal_lines
            where run_id = %s
            order by id
            """,
            (run_id,),
        )
        rows = cur.fetchall()
    return [
        GLJournalLine(
            id=str(row["id"]),
            entry_date=row["entry_date"],
            account_code=row["account_code"],
            debit_amount=Decimal(str(row["debit_amount"])),
            credit_amount=Decimal(str(row["credit_amount"])),
            description=row.get("description"),
        )
        for row in rows
    ]


def _load_gl_bucket_accounts(conn: Connection, client_id: str) -> dict[str, set[str]]:
    buckets = {
        "net_pay_control": set(),
        "taxes": set(),
        "pension": set(),
        "other": set(),
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            select bucket, account_code
            from public.client_gl_bucket_accounts
            where client_id = %s
            order by bucket, account_code
            """,
            (client_id,),
        )
        for row in cur.fetchall():
            buckets[str(row["bucket"])].add(row["account_code"])
    return buckets


def _run_locked(conn: Connection, run_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("select locked_at from public.runs where id = %s", (run_id,))
        row = cur.fetchone()
        return bool(row and row["locked_at"] is not None)


def _upsert_run_import_health_summary(
    conn: Connection,
    *,
    run_id: str,
    firm_id: str,
    client_id: str,
    actor_user_id: str | None,
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select file_kind, mapping_template_id
            from public.source_files
            where run_id = %s
            """,
            (run_id,),
        )
        files = cur.fetchall()
        present_kinds = {row["file_kind"] for row in files}
        mapped_kinds = {row["file_kind"] for row in files if row["mapping_template_id"] is not None}

        cur.execute(
            """
            select count(*)::int as total
            from public.variances
            where run_id = %s
              and category = 'import'
              and code = 'IMP-001'
              and status = 'open'
            """,
            (run_id,),
        )
        drift_row = cur.fetchone() or {"total": 0}
        open_drift = int(drift_row.get("total", 0))

        cur.execute(
            """
            select count(*)::int as total_jobs,
                   count(*) filter (where status = 'failed')::int as failed_jobs
            from public.jobs
            where run_id = %s
            """,
            (run_id,),
        )
        jobs = cur.fetchone() or {"total_jobs": 0, "failed_jobs": 0}
        total_jobs = int(jobs.get("total_jobs", 0))
        failed_jobs = int(jobs.get("failed_jobs", 0))

    score, band, factors = compute_import_health_score(
        present_file_kinds=present_kinds,
        mapped_file_kinds=mapped_kinds,
        open_drift_blockers=open_drift,
        failed_jobs=failed_jobs,
        total_jobs=total_jobs,
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into public.run_import_health_summaries(
              run_id, firm_id, client_id, health_score, health_band, factors, computed_at
            )
            values (%s, %s, %s, %s, %s::app.import_health_band, %s::jsonb, now())
            on conflict (run_id)
            do update set health_score = excluded.health_score,
                          health_band = excluded.health_band,
                          factors = excluded.factors,
                          computed_at = excluded.computed_at
            returning run_id, health_score, health_band, factors, computed_at
            """,
            (
                run_id,
                firm_id,
                client_id,
                score,
                band,
                json.dumps(factors, sort_keys=True),
            ),
        )
        summary = cur.fetchone()

    _audit(
        conn,
        firm_id=firm_id,
        client_id=client_id,
        run_id=run_id,
        event_type="import.health.scored",
        actor_user_id=actor_user_id,
        entity_type="run",
        entity_id=run_id,
        payload={"health_score": score, "health_band": band, "factors": factors},
    )
    return summary


def _persist_bank_result(
    conn: Connection,
    *,
    run_id: str,
    firm_id: str,
    client_id: str,
    policy: ReconPolicy,
    result: Any,
    actor_user_id: str | None,
    timing_snapshot: dict[str, Any] | None = None,
    as_of_date: date | None = None,
    payday_date: date | None = None,
) -> None:
    effective_as_of = as_of_date or date.today()
    effective_timing = timing_snapshot or {}
    with conn.cursor() as cur:
        cur.execute(
            "delete from public.match_group_members where match_group_id in (select id from public.match_groups where run_id = %s)",
            (run_id,),
        )
        cur.execute("delete from public.match_groups where run_id = %s", (run_id,))
        cur.execute("delete from public.variances where run_id = %s and category = 'bank'", (run_id,))

        for group in result.match_groups:
            cur.execute(
                """
                insert into public.match_groups(
                  id, run_id, firm_id, client_id, group_kind, match_confidence,
                  expected_total, bank_total, delta, matched_on, algorithm_version
                )
                values (%s::uuid, %s, %s, %s, %s::app.match_group_kind, %s::app.match_confidence,
                        %s, %s, %s, %s, 'bank_v1')
                """,
                (
                    group.id,
                    run_id,
                    firm_id,
                    client_id,
                    group.group_kind,
                    group.match_confidence,
                    group.expected_total,
                    group.bank_total,
                    group.delta,
                    group.matched_on,
                ),
            )
            for expected_id in group.expected_ids:
                cur.execute(
                    """
                    insert into public.match_group_members(match_group_id, member_type, member_id, signed_amount)
                    values (%s::uuid, 'payroll_expected', %s::uuid,
                      (select net_amount from public.payroll_expected where id = %s::uuid))
                    """,
                    (group.id, expected_id, expected_id),
                )
            for bank_id in group.bank_ids:
                cur.execute(
                    """
                    insert into public.match_group_members(match_group_id, member_type, member_id, signed_amount)
                    values (%s::uuid, 'bank_transaction', %s::uuid,
                      (select amount_signed from public.bank_transactions where id = %s::uuid))
                    """,
                    (group.id, bank_id, bank_id),
                )
            _audit(
                conn,
                firm_id=firm_id,
                client_id=client_id,
                run_id=run_id,
                event_type="recon.bank.match_group.created",
                actor_user_id=actor_user_id,
                entity_type="match_group",
                entity_id=group.id,
                payload=group.to_dict(),
            )

        for variance in result.variances:
            cur.execute(
                """
                insert into public.variances(
                  id, run_id, firm_id, client_id, code, category, severity, status,
                  message, amount, event_date, account_ref, details
                )
                values (%s::uuid, %s, %s, %s, %s, %s, %s::app.variance_severity,
                        %s::app.variance_status, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    variance.id,
                    run_id,
                    firm_id,
                    client_id,
                    variance.code,
                    variance.category,
                    variance.severity,
                    variance.status,
                    variance.message,
                    variance.amount,
                    variance.event_date,
                    variance.account_ref,
                    json.dumps(variance.details, sort_keys=True),
                ),
            )
            _audit(
                conn,
                firm_id=firm_id,
                client_id=client_id,
                run_id=run_id,
                event_type="recon.bank.variance.created",
                actor_user_id=actor_user_id,
                entity_type="variance",
                entity_id=variance.id,
                payload=variance.to_dict(),
            )

        cur.execute(
            """
            insert into public.run_bank_tieout_summaries(
              run_id, firm_id, client_id,
              expected_net_pay, matched_bank_total, delta, status,
              policy_snapshot, computed_at, updated_at
            )
            values (%s, %s, %s, %s, %s, %s, %s::app.tieout_status,
                    %s::jsonb, now(), now())
            on conflict (run_id)
            do update set expected_net_pay = excluded.expected_net_pay,
                          matched_bank_total = excluded.matched_bank_total,
                          delta = excluded.delta,
                          status = excluded.status,
                          policy_snapshot = excluded.policy_snapshot,
                          computed_at = excluded.computed_at,
                          updated_at = excluded.updated_at
            """,
            (
                run_id,
                firm_id,
                client_id,
                result.summary.expected_net_pay,
                result.summary.matched_bank_total,
                result.summary.delta,
                result.summary.status,
                json.dumps(
                    {
                        "amount_tolerance": f"{policy.amount_tolerance:.2f}",
                        "date_window_days": policy.date_window_days,
                        "max_group_size": policy.max_group_size,
                        "enable_one_to_many": policy.enable_one_to_many,
                        "enable_many_to_one": policy.enable_many_to_one,
                        "require_allowed_account": policy.require_allowed_account,
                        "as_of_date": effective_as_of.isoformat(),
                        "timing": effective_timing,
                    },
                    sort_keys=True,
                ),
            ),
        )
        cur.execute(
            """
            update public.runs
            set status = 'completed',
                payday_date = %s,
                must_close_by_date = %s,
                sla_reminder_state = %s,
                updated_at = now()
            where id = %s
            """,
            (payday_date, payday_date, _sla_state(effective_as_of, payday_date), run_id),
        )


def _persist_gl_result(
    conn: Connection,
    *,
    run_id: str,
    firm_id: str,
    client_id: str,
    result: Any,
    actor_user_id: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute("delete from public.variances where run_id = %s and category = 'gl'", (run_id,))

        for variance in result.variances:
            cur.execute(
                """
                insert into public.variances(
                  id, run_id, firm_id, client_id, code, category, severity, status,
                  message, amount, event_date, account_ref, details
                )
                values (%s::uuid, %s, %s, %s, %s, %s, %s::app.variance_severity,
                        %s::app.variance_status, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    variance.id,
                    run_id,
                    firm_id,
                    client_id,
                    variance.code,
                    variance.category,
                    variance.severity,
                    variance.status,
                    variance.message,
                    variance.amount,
                    variance.event_date,
                    variance.account_ref,
                    json.dumps(variance.details, sort_keys=True),
                ),
            )
            _audit(
                conn,
                firm_id=firm_id,
                client_id=client_id,
                run_id=run_id,
                event_type="recon.gl.variance.created",
                actor_user_id=actor_user_id,
                entity_type="variance",
                entity_id=variance.id,
                payload=variance.to_dict(),
            )

        cur.execute(
            """
            insert into public.run_gl_tieout_summaries(
              run_id, firm_id, client_id, payroll_totals, gl_totals,
              deltas, is_balanced, status, rules_used, computed_at, updated_at
            )
            values (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s, %s::app.tieout_status, %s::jsonb, now(), now())
            on conflict (run_id)
            do update set payroll_totals = excluded.payroll_totals,
                          gl_totals = excluded.gl_totals,
                          deltas = excluded.deltas,
                          is_balanced = excluded.is_balanced,
                          status = excluded.status,
                          rules_used = excluded.rules_used,
                          computed_at = excluded.computed_at,
                          updated_at = excluded.updated_at
            """,
            (
                run_id,
                firm_id,
                client_id,
                json.dumps(result.summary.to_dict()["payroll_totals"], sort_keys=True),
                json.dumps(result.summary.to_dict()["gl_totals"], sort_keys=True),
                json.dumps(result.summary.to_dict()["deltas"], sort_keys=True),
                result.summary.is_balanced,
                result.summary.status,
                json.dumps(result.summary.rules_used, sort_keys=True),
            ),
        )
        cur.execute("update public.runs set status = 'completed', updated_at = now() where id = %s", (run_id,))


def _upsert_export_pack(
    conn: Connection,
    *,
    run_id: str,
    firm_id: str,
    client_id: str,
    status_value: str,
    pack_hash: str | None,
    storage_bucket: str | None,
    storage_path: str | None,
    manifest: dict[str, Any] | None,
    error: dict[str, Any] | None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into public.export_packs(
              run_id, firm_id, client_id, status, pack_hash,
              storage_bucket, storage_path, deterministic_manifest,
              generated_at, error, updated_at
            )
            values (%s, %s, %s, %s::app.export_pack_status, %s,
                    %s, %s, %s::jsonb, now(), %s::jsonb, now())
            on conflict (run_id, pack_hash)
            where pack_hash is not null
            do update set status = excluded.status,
                          storage_bucket = excluded.storage_bucket,
                          storage_path = excluded.storage_path,
                          deterministic_manifest = excluded.deterministic_manifest,
                          generated_at = excluded.generated_at,
                          error = excluded.error,
                          updated_at = excluded.updated_at
            returning id
            """,
            (
                run_id,
                firm_id,
                client_id,
                status_value,
                pack_hash,
                storage_bucket,
                storage_path,
                json.dumps(manifest or {}, sort_keys=True, default=str),
                json.dumps(error or {}, sort_keys=True, default=str),
            ),
        )
        row = cur.fetchone()
        if row:
            return str(row["id"])

        # If pack hash is null, fall back to latest row for run.
        cur.execute(
            """
            insert into public.export_packs(
              run_id, firm_id, client_id, status, pack_hash,
              storage_bucket, storage_path, deterministic_manifest,
              generated_at, error, updated_at
            )
            values (%s, %s, %s, %s::app.export_pack_status, %s,
                    %s, %s, %s::jsonb, now(), %s::jsonb, now())
            returning id
            """,
            (
                run_id,
                firm_id,
                client_id,
                status_value,
                pack_hash,
                storage_bucket,
                storage_path,
                json.dumps(manifest or {}, sort_keys=True, default=str),
                json.dumps(error or {}, sort_keys=True, default=str),
            ),
        )
        return str(cur.fetchone()["id"])


def _load_run_export_inputs(conn: Connection, run_id: str) -> tuple[dict[str, Any], dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select r.id, r.status as run_status, r.payday_date, r.must_close_by_date, r.sla_reminder_state,
                   b.expected_net_pay, b.matched_bank_total, b.delta, b.status as bank_status, b.computed_at as bank_computed_at, b.policy_snapshot,
                   g.status as gl_status, g.payroll_totals, g.gl_totals, g.deltas, g.rules_used, g.computed_at as gl_computed_at
            from public.runs r
            left join public.run_bank_tieout_summaries b on b.run_id = r.id
            left join public.run_gl_tieout_summaries g on g.run_id = r.id
            where r.id = %s
            """,
            (run_id,),
        )
        summary_row = cur.fetchone()
        if not summary_row:
            raise RuntimeError("Run not found for export")

        cur.execute(
            """
            select id, code, category, severity, status, message, amount,
                   event_date, account_ref, note, changed_by, changed_at
            from public.variances
            where run_id = %s
            order by code, id
            """,
            (run_id,),
        )
        variances = [dict(row) for row in cur.fetchall()]

        cur.execute(
            """
            select id, event_type, actor_user_id, created_at, entity_type, entity_id
            from public.audit_events
            where run_id = %s
              and event_type not like 'export.pack.%%'
            order by created_at, id
            limit 500
            """,
            (run_id,),
        )
        audit_rows = [dict(row) for row in cur.fetchall()]

        cur.execute(
            """
            select id, file_kind, checksum_sha256, created_at, uri
            from public.source_files
            where run_id = %s
            order by file_kind, id
            """,
            (run_id,),
        )
        source_files = [dict(row) for row in cur.fetchall()]

        cur.execute(
            """
            select status, prepared_by, prepared_at, reviewer_id, reviewed_at
            from public.approvals
            where run_id = %s
            """,
            (run_id,),
        )
        approval = cur.fetchone()

        cur.execute(
            """
            select health_score, health_band, factors, computed_at
            from public.run_import_health_summaries
            where run_id = %s
            """,
            (run_id,),
        )
        import_health = cur.fetchone()

    summary = {
        "run_status": summary_row["run_status"],
        "payday_date": summary_row.get("payday_date"),
        "must_close_by_date": summary_row.get("must_close_by_date"),
        "sla_reminder_state": summary_row.get("sla_reminder_state"),
        "tieout": {
            "expected_net_pay": summary_row["expected_net_pay"],
            "matched_bank_total": summary_row["matched_bank_total"],
            "delta": summary_row["delta"],
            "status": summary_row["bank_status"],
            "computed_at": summary_row["bank_computed_at"],
            "policy_snapshot": summary_row.get("policy_snapshot") or {},
        },
        "gl_tieout": {
            "status": summary_row["gl_status"],
            "payroll_totals": summary_row["payroll_totals"],
            "gl_totals": summary_row["gl_totals"],
            "deltas": summary_row["deltas"],
            "rules_used": summary_row["rules_used"],
            "computed_at": summary_row["gl_computed_at"],
        },
        "open_variances": {
            "blocker": sum(1 for row in variances if row["status"] == "open" and row["severity"] == "blocker"),
            "review": sum(1 for row in variances if row["status"] == "open" and row["severity"] == "review"),
        },
        "import_health": dict(import_health) if import_health else None,
        "drift_events": {
            "count": sum(1 for row in audit_rows if row["event_type"] == "import.drift.detected"),
            "latest_event_at": max(
                (row["created_at"] for row in audit_rows if row["event_type"] == "import.drift.detected"),
                default=None,
            ),
        },
    }

    signoff = dict(approval) if approval else {}
    gl_summary = summary["gl_tieout"] if summary_row.get("gl_status") is not None else None
    return summary, gl_summary, variances, audit_rows, source_files, signoff


def _mark_job_complete(conn: Connection, job_id: str, result_payload: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update public.jobs
            set status = 'succeeded',
                finished_at = now(),
                result = %s::jsonb,
                error = null
            where id = %s
            """,
            (json.dumps(result_payload, sort_keys=True), job_id),
        )


def _mark_job_failed(conn: Connection, job: dict[str, Any], error: Exception) -> None:
    error_payload = {"message": str(error), "type": type(error).__name__}
    attempt_count = int(job.get("attempt_count", 1))
    max_attempts = int(job.get("max_attempts", 1))

    with conn.cursor() as cur:
        if attempt_count < max_attempts:
            cur.execute(
                """
            update public.jobs
            set status = 'queued',
                started_at = null,
                finished_at = null,
                error = %s::jsonb,
                attempt_count = %s
            where id = %s
            """,
                (json.dumps(error_payload, sort_keys=True), attempt_count, job["id"]),
            )
            _audit(
                conn,
                firm_id=str(job["firm_id"]),
                client_id=str(job["client_id"]),
                run_id=str(job["run_id"]),
                event_type="job.retry_scheduled",
                actor_user_id=str(job.get("created_by")) if job.get("created_by") else None,
                entity_type="job",
                entity_id=str(job["id"]),
                payload={"attempt_count": attempt_count, "max_attempts": max_attempts, "error": error_payload},
            )
            _upsert_run_import_health_summary(
                conn,
                run_id=str(job["run_id"]),
                firm_id=str(job["firm_id"]),
                client_id=str(job["client_id"]),
                actor_user_id=str(job.get("created_by")) if job.get("created_by") else None,
            )
            return

        cur.execute(
            """
            update public.jobs
            set status = 'failed',
                finished_at = now(),
                error = %s::jsonb,
                attempt_count = %s
            where id = %s
            """,
                (json.dumps(error_payload, sort_keys=True), attempt_count, job["id"]),
        )
        if job["job_type"] in ("reconcile_bank", "reconcile_gl"):
            cur.execute("update public.runs set status = 'failed', updated_at = now() where id = %s", (job["run_id"],))

    _audit(
        conn,
        firm_id=str(job["firm_id"]),
        client_id=str(job["client_id"]),
        run_id=str(job["run_id"]),
        event_type=f"{job['job_type']}.failed",
        actor_user_id=str(job.get("created_by")) if job.get("created_by") else None,
        entity_type="job",
        entity_id=str(job["id"]),
        payload=error_payload,
    )
    _upsert_run_import_health_summary(
        conn,
        run_id=str(job["run_id"]),
        firm_id=str(job["firm_id"]),
        client_id=str(job["client_id"]),
        actor_user_id=str(job.get("created_by")) if job.get("created_by") else None,
    )


def process_job(conn: Connection, job: dict[str, Any]) -> None:
    actor = str(job.get("created_by")) if job.get("created_by") else None
    run_id = str(job["run_id"])
    firm_id = str(job["firm_id"])
    client_id = str(job["client_id"])

    if job["job_type"] == "noop":
        if _run_locked(conn, run_id):
            raise PermissionError("Run is locked")
        with conn.cursor() as cur:
            cur.execute("update public.runs set status = 'completed', updated_at = now() where id = %s", (run_id,))
        _mark_job_complete(conn, str(job["id"]), {"noop": True})
        _upsert_run_import_health_summary(
            conn,
            run_id=run_id,
            firm_id=firm_id,
            client_id=client_id,
            actor_user_id=actor,
        )
        _audit(
            conn,
            firm_id=firm_id,
            client_id=client_id,
            run_id=run_id,
            event_type="job.completed",
            actor_user_id=actor,
            entity_type="job",
            entity_id=str(job["id"]),
            payload={"job_type": "noop"},
        )
        return

    if job["job_type"] == "reconcile_bank":
        if _run_locked(conn, run_id):
            raise PermissionError("Run is locked")

        _audit(
            conn,
            firm_id=firm_id,
            client_id=client_id,
            run_id=run_id,
            event_type="recon.bank.started",
            actor_user_id=actor,
            entity_type="job",
            entity_id=str(job["id"]),
        )

        policy, allowed_accounts = _load_policy(conn, client_id)
        run_meta = _load_run_meta(conn, run_id)
        as_of_date = _parse_as_of_date(job.get("payload") or {})
        timing_policy = _load_uk_timing_policy(conn, client_id)
        expected_items = _load_payroll_expected(conn, run_id)
        bank_items = _load_bank_transactions(conn, run_id)
        result = reconcile_bank(
            run_id=run_id,
            expected_items=expected_items,
            bank_items=bank_items,
            policy=policy,
            allowed_accounts=allowed_accounts,
        )
        timing_variances, timing_snapshot = build_timing_variances(
            run_id=run_id,
            payroll_expected=expected_items,
            period_end=run_meta["period_end"],
            as_of_date=as_of_date,
            policy=timing_policy,
        )
        if timing_variances:
            for variance in timing_variances:
                _audit(
                    conn,
                    firm_id=firm_id,
                    client_id=client_id,
                    run_id=run_id,
                    event_type="recon.timing.variance.created",
                    actor_user_id=actor,
                    entity_type="variance",
                    entity_id=variance.id,
                    payload=variance.to_dict(),
                )
        result.variances.extend(timing_variances)
        result.variances.sort(key=lambda item: (item.code, item.event_date or date.min, item.id))
        has_blocker = any(item.severity == "blocker" and item.status == "open" for item in result.variances)
        has_review = any(item.severity == "review" and item.status == "open" for item in result.variances)
        if has_blocker:
            result.summary.status = "Not tied"
        elif has_review:
            result.summary.status = "Needs review"
        elif abs(result.summary.delta) <= policy.amount_tolerance:
            result.summary.status = "Tied"
        else:
            result.summary.status = "Not tied"
        payday_date = max((item.payment_date for item in expected_items), default=None)

        _persist_bank_result(
            conn,
            run_id=run_id,
            firm_id=firm_id,
            client_id=client_id,
            policy=policy,
            timing_snapshot=timing_snapshot,
            as_of_date=as_of_date,
            payday_date=payday_date,
            result=result,
            actor_user_id=actor,
        )
        payload = {
            "summary": result.summary.to_dict(),
            "match_groups": len(result.match_groups),
            "variances": len(result.variances),
            "as_of_date": as_of_date.isoformat(),
        }
        _mark_job_complete(conn, str(job["id"]), payload)
        _upsert_run_import_health_summary(
            conn,
            run_id=run_id,
            firm_id=firm_id,
            client_id=client_id,
            actor_user_id=actor,
        )
        _audit(
            conn,
            firm_id=firm_id,
            client_id=client_id,
            run_id=run_id,
            event_type="recon.bank.completed",
            actor_user_id=actor,
            entity_type="job",
            entity_id=str(job["id"]),
            payload=payload,
        )
        return

    if job["job_type"] == "reconcile_gl":
        if _run_locked(conn, run_id):
            raise PermissionError("Run is locked")

        _audit(
            conn,
            firm_id=firm_id,
            client_id=client_id,
            run_id=run_id,
            event_type="recon.gl.started",
            actor_user_id=actor,
            entity_type="job",
            entity_id=str(job["id"]),
        )

        policy, _ = _load_policy(conn, client_id)
        payroll_expected = _load_payroll_expected(conn, run_id)
        gl_lines = _load_gl_journal_lines(conn, run_id)
        bucket_accounts = _load_gl_bucket_accounts(conn, client_id)
        gl_result = reconcile_gl(
            run_id=run_id,
            payroll_expected=payroll_expected,
            gl_lines=gl_lines,
            bucket_accounts=bucket_accounts,
            tolerance=policy.amount_tolerance,
        )

        _persist_gl_result(
            conn,
            run_id=run_id,
            firm_id=firm_id,
            client_id=client_id,
            result=gl_result,
            actor_user_id=actor,
        )
        payload = {
            "summary": gl_result.summary.to_dict(),
            "variances": len(gl_result.variances),
        }
        _mark_job_complete(conn, str(job["id"]), payload)
        _upsert_run_import_health_summary(
            conn,
            run_id=run_id,
            firm_id=firm_id,
            client_id=client_id,
            actor_user_id=actor,
        )
        _audit(
            conn,
            firm_id=firm_id,
            client_id=client_id,
            run_id=run_id,
            event_type="recon.gl.completed",
            actor_user_id=actor,
            entity_type="job",
            entity_id=str(job["id"]),
            payload=payload,
        )
        return

    if job["job_type"] == "export_pack":
        summary, gl_summary, variances, audit_rows, source_files, signoff = _load_run_export_inputs(conn, run_id)

        blob, pack_hash, manifest = build_export_pack(
            run_id=run_id,
            summary=summary,
            gl_summary=gl_summary,
            variances=variances,
            audit_events=audit_rows,
            source_files=source_files,
            signoff=signoff,
        )

        storage_bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "audit-packs")
        storage_path = f"runs/{run_id}/audit-pack-{pack_hash}.zip"

        storage_client = get_storage_client()
        storage_uri = storage_client.upload_bytes(storage_bucket, storage_path, blob, "application/zip")

        export_pack_id = _upsert_export_pack(
            conn,
            run_id=run_id,
            firm_id=firm_id,
            client_id=client_id,
            status_value="generated",
            pack_hash=pack_hash,
            storage_bucket=storage_bucket,
            storage_path=storage_path,
            manifest=manifest,
            error=None,
        )

        payload = {
            "export_pack_id": export_pack_id,
            "pack_hash": pack_hash,
            "storage_uri": storage_uri,
        }
        _mark_job_complete(conn, str(job["id"]), payload)
        _audit(
            conn,
            firm_id=firm_id,
            client_id=client_id,
            run_id=run_id,
            event_type="export.pack.generated",
            actor_user_id=actor,
            entity_type="export_pack",
            entity_id=export_pack_id,
            payload=payload,
        )
        return

    _mark_job_complete(conn, str(job["id"]), {"skipped": True, "reason": "unsupported job type"})


def run_once() -> bool:
    with _connect() as conn:
        job: dict[str, Any] | None = None
        try:
            job = claim_next_job(conn)
            if not job:
                conn.commit()
                return False
            # Persist claim transition (queued -> running + attempt increment)
            # before processing to avoid losing retry state on handler errors.
            conn.commit()
            with conn.transaction():
                process_job(conn, job)
            return True
        except Exception as exc:
            conn.rollback()
            try:
                if job is not None:
                    with conn.transaction():
                        if job.get("job_type") == "export_pack":
                            _upsert_export_pack(
                                conn,
                                run_id=str(job["run_id"]),
                                firm_id=str(job["firm_id"]),
                                client_id=str(job["client_id"]),
                                status_value="failed",
                                pack_hash=None,
                                storage_bucket=None,
                                storage_path=None,
                                manifest=None,
                                error={"message": str(exc), "type": type(exc).__name__},
                            )
                        _mark_job_failed(conn, job, exc)
            except Exception:
                conn.rollback()
            # A job was claimed even if processing failed; signal work happened
            # so caller loops can continue draining/retrying the queue.
            return job is not None


def main() -> None:
    parser = argparse.ArgumentParser(description="Tally worker")
    parser.add_argument("--once", action="store_true", help="Process one queued job then exit")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Polling interval seconds")
    args = parser.parse_args()

    if args.once:
        run_once()
        return

    while True:
        processed = run_once()
        if not processed:
            time.sleep(args.poll_interval)


if __name__ == "__main__":  # pragma: no cover
    main()
