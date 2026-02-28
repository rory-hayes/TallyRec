from __future__ import annotations

import argparse
import json
import os
import time
from datetime import date
from decimal import Decimal
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row

from libs.core.engine import BankTransaction, PayrollExpected, ReconPolicy, reconcile_bank

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


def _load_canonical(conn: Connection, run_id: str) -> tuple[list[PayrollExpected], list[BankTransaction]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, payment_date, net_amount, employee_ref
            from public.payroll_expected
            where run_id = %s
            order by id
            """,
            (run_id,),
        )
        expected_rows = cur.fetchall()

        cur.execute(
            """
            select id, posted_date, amount_signed, account_ref, description
            from public.bank_transactions
            where run_id = %s
            order by id
            """,
            (run_id,),
        )
        bank_rows = cur.fetchall()

    expected = [
        PayrollExpected(
            id=str(row["id"]),
            payment_date=row["payment_date"],
            net_amount=Decimal(str(row["net_amount"])),
            employee_ref=row.get("employee_ref"),
        )
        for row in expected_rows
    ]
    bank = [
        BankTransaction(
            id=str(row["id"]),
            posted_date=row["posted_date"],
            amount_signed=Decimal(str(row["amount_signed"])),
            account_ref=row.get("account_ref"),
            description=row.get("description"),
        )
        for row in bank_rows
    ]
    return expected, bank


def _persist_result(
    conn: Connection,
    *,
    run_id: str,
    firm_id: str,
    client_id: str,
    policy: ReconPolicy,
    result: Any,
    actor_user_id: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute("delete from public.match_group_members where match_group_id in (select id from public.match_groups where run_id = %s)", (run_id,))
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
                    },
                    sort_keys=True,
                ),
            ),
        )
        cur.execute("update public.runs set status = 'completed', updated_at = now() where id = %s", (run_id,))


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
    with conn.cursor() as cur:
        cur.execute(
            """
            update public.jobs
            set status = 'failed',
                finished_at = now(),
                error = %s::jsonb
            where id = %s
            """,
            (json.dumps(error_payload, sort_keys=True), job["id"]),
        )
        cur.execute("update public.runs set status = 'failed', updated_at = now() where id = %s", (job["run_id"],))
    _audit(
        conn,
        firm_id=str(job["firm_id"]),
        client_id=str(job["client_id"]),
        run_id=str(job["run_id"]),
        event_type="recon.bank.failed" if job["job_type"] == "reconcile_bank" else "job.failed",
        actor_user_id=str(job.get("created_by")) if job.get("created_by") else None,
        entity_type="job",
        entity_id=str(job["id"]),
        payload=error_payload,
    )


def process_job(conn: Connection, job: dict[str, Any]) -> None:
    actor = str(job.get("created_by")) if job.get("created_by") else None
    if job["job_type"] == "noop":
        with conn.cursor() as cur:
            cur.execute("update public.runs set status = 'completed', updated_at = now() where id = %s", (job["run_id"],))
        _mark_job_complete(conn, str(job["id"]), {"noop": True})
        _audit(
            conn,
            firm_id=str(job["firm_id"]),
            client_id=str(job["client_id"]),
            run_id=str(job["run_id"]),
            event_type="job.completed",
            actor_user_id=actor,
            entity_type="job",
            entity_id=str(job["id"]),
            payload={"job_type": "noop"},
        )
        return

    if job["job_type"] != "reconcile_bank":
        _mark_job_complete(conn, str(job["id"]), {"skipped": True, "reason": "unsupported job type"})
        return

    _audit(
        conn,
        firm_id=str(job["firm_id"]),
        client_id=str(job["client_id"]),
        run_id=str(job["run_id"]),
        event_type="recon.bank.started",
        actor_user_id=actor,
        entity_type="job",
        entity_id=str(job["id"]),
    )

    policy, allowed_accounts = _load_policy(conn, str(job["client_id"]))
    expected_items, bank_items = _load_canonical(conn, str(job["run_id"]))
    result = reconcile_bank(
        run_id=str(job["run_id"]),
        expected_items=expected_items,
        bank_items=bank_items,
        policy=policy,
        allowed_accounts=allowed_accounts,
    )

    _persist_result(
        conn,
        run_id=str(job["run_id"]),
        firm_id=str(job["firm_id"]),
        client_id=str(job["client_id"]),
        policy=policy,
        result=result,
        actor_user_id=actor,
    )
    payload = {
        "summary": result.summary.to_dict(),
        "match_groups": len(result.match_groups),
        "variances": len(result.variances),
    }
    _mark_job_complete(conn, str(job["id"]), payload)
    _audit(
        conn,
        firm_id=str(job["firm_id"]),
        client_id=str(job["client_id"]),
        run_id=str(job["run_id"]),
        event_type="recon.bank.completed",
        actor_user_id=actor,
        entity_type="job",
        entity_id=str(job["id"]),
        payload=payload,
    )


def run_once() -> bool:
    with _connect() as conn:
        job: dict[str, Any] | None = None
        try:
            job = claim_next_job(conn)
            if not job:
                conn.commit()
                return False
            process_job(conn, job)
            conn.commit()
            return True
        except Exception as exc:
            conn.rollback()
            try:
                if job is not None:
                    with conn:
                        _mark_job_failed(conn, job, exc)
            except Exception:
                conn.rollback()
            return False


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


if __name__ == "__main__":
    main()
