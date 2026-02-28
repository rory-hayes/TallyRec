from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from psycopg import Connection

from apps.api.app.db import db_session
from apps.api.app.schemas.api import (
    CreateClientRequest,
    CreateClientResponse,
    CreateFirmRequest,
    CreateFirmResponse,
    CreateRunRequest,
    CreateRunResponse,
    EnqueueJobRequest,
    EnqueueJobResponse,
    PutBankAccountsRequest,
    ReconcileBankRequest,
    RegisterSourceFileRequest,
    RegisterSourceFileResponse,
    UpdateReconPolicyRequest,
)

router = APIRouter(prefix="/v1", tags=["v1"])


def get_user_id(x_user_id: str | None = Header(default=None)) -> str:
    if not x_user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-User-Id header")
    return x_user_id


def _get_run(conn: Connection, run_id: UUID) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, firm_id, client_id, status
            from public.runs
            where id = %s
            """,
            (str(run_id),),
        )
        return cur.fetchone()


def _audit(
    conn: Connection,
    *,
    firm_id: str | None,
    client_id: str | None,
    run_id: str | None,
    event_type: str,
    actor_user_id: str,
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


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/firms", response_model=CreateFirmResponse, status_code=status.HTTP_201_CREATED)
def create_firm(payload: CreateFirmRequest, user_id: str = Depends(get_user_id)) -> Any:
    firm_id = str(uuid4())
    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into public.firms(id, name, slug)
                values (%s::uuid, %s, %s)
                """,
                (firm_id, payload.name, payload.slug),
            )
            cur.execute(
                """
                insert into public.firm_memberships(firm_id, user_id, role)
                values (%s, %s::uuid, 'owner')
                """,
                (firm_id, user_id),
            )
            cur.execute(
                """
                select id, name, slug, created_at
                from public.firms
                where id = %s::uuid
                """,
                (firm_id,),
            )
            firm = cur.fetchone()
        _audit(
            conn,
            firm_id=firm_id,
            client_id=None,
            run_id=None,
            event_type="firm.created",
            actor_user_id=user_id,
            entity_type="firm",
            entity_id=firm_id,
            payload={"slug": payload.slug},
        )
        return firm


@router.post("/clients", response_model=CreateClientResponse, status_code=status.HTTP_201_CREATED)
def create_client(payload: CreateClientRequest, user_id: str = Depends(get_user_id)) -> Any:
    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute("select id from public.firms where id = %s", (str(payload.firm_id),))
            firm = cur.fetchone()
            if not firm:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Firm not found")
            cur.execute(
                """
                insert into public.clients(firm_id, name, external_ref)
                values (%s, %s, %s)
                returning id, firm_id, name, external_ref, created_at
                """,
                (str(payload.firm_id), payload.name, payload.external_ref),
            )
            client = cur.fetchone()
            cur.execute(
                """
                insert into public.client_recon_policies(client_id, firm_id)
                values (%s, %s)
                on conflict (client_id) do nothing
                """,
                (client["id"], payload.firm_id),
            )
        _audit(
            conn,
            firm_id=str(payload.firm_id),
            client_id=str(client["id"]),
            run_id=None,
            event_type="client.created",
            actor_user_id=user_id,
            entity_type="client",
            entity_id=str(client["id"]),
        )
        return client


@router.post("/runs", response_model=CreateRunResponse, status_code=status.HTTP_201_CREATED)
def create_run(payload: CreateRunRequest, user_id: str = Depends(get_user_id)) -> Any:
    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select id from public.clients
                where id = %s and firm_id = %s
                """,
                (str(payload.client_id), str(payload.firm_id)),
            )
            client = cur.fetchone()
            if not client:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")

            cur.execute(
                """
                insert into public.runs(firm_id, client_id, period_start, period_end, created_by)
                values (%s, %s, %s, %s, %s::uuid)
                returning id, firm_id, client_id, status, period_start, period_end, created_at
                """,
                (
                    str(payload.firm_id),
                    str(payload.client_id),
                    payload.period_start,
                    payload.period_end,
                    user_id,
                ),
            )
            run = cur.fetchone()
        _audit(
            conn,
            firm_id=str(run["firm_id"]),
            client_id=str(run["client_id"]),
            run_id=str(run["id"]),
            event_type="run.created",
            actor_user_id=user_id,
            entity_type="run",
            entity_id=str(run["id"]),
        )
        return run


@router.post("/runs/{run_id}/source-files", response_model=RegisterSourceFileResponse, status_code=status.HTTP_201_CREATED)
def register_source_file(run_id: UUID, payload: RegisterSourceFileRequest, user_id: str = Depends(get_user_id)) -> Any:
    with db_session(user_id) as conn:
        run = _get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into public.source_files(
                  run_id, firm_id, client_id, file_kind, filename, uri,
                  checksum_sha256, byte_size, mapping_template_id, registered_by
                )
                values (%s, %s, %s, %s::app.file_kind, %s, %s, %s, %s, %s, %s::uuid)
                returning id, run_id, file_kind, uri, checksum_sha256, created_at
                """,
                (
                    str(run_id),
                    run["firm_id"],
                    run["client_id"],
                    payload.file_kind,
                    payload.filename,
                    payload.uri,
                    payload.checksum_sha256,
                    payload.byte_size,
                    str(payload.mapping_template_id) if payload.mapping_template_id else None,
                    user_id,
                ),
            )
            source_file = cur.fetchone()
        _audit(
            conn,
            firm_id=str(run["firm_id"]),
            client_id=str(run["client_id"]),
            run_id=str(run_id),
            event_type="source_file.registered",
            actor_user_id=user_id,
            entity_type="source_file",
            entity_id=str(source_file["id"]),
        )
        return source_file


def _enqueue_job(conn: Connection, run: dict[str, Any], user_id: str, payload: EnqueueJobRequest) -> dict[str, Any]:
    with conn.cursor() as cur:
        if payload.idempotency_key:
            cur.execute(
                """
                select id, run_id, status, job_type, queued_at
                from public.jobs
                where run_id = %s and idempotency_key = %s
                """,
                (run["id"], payload.idempotency_key),
            )
            existing = cur.fetchone()
            if existing:
                return existing

        cur.execute(
            """
            insert into public.jobs(
              run_id, firm_id, client_id, job_type, payload, idempotency_key, created_by
            )
            values (%s, %s, %s, %s::app.job_type, %s::jsonb, %s, %s::uuid)
            returning id, run_id, status, job_type, queued_at
            """,
            (
                run["id"],
                run["firm_id"],
                run["client_id"],
                payload.job_type,
                json.dumps(payload.payload, sort_keys=True),
                payload.idempotency_key,
                user_id,
            ),
        )
        return cur.fetchone()


@router.post("/runs/{run_id}/jobs", response_model=EnqueueJobResponse, status_code=status.HTTP_201_CREATED)
def enqueue_job(run_id: UUID, payload: EnqueueJobRequest, user_id: str = Depends(get_user_id)) -> Any:
    with db_session(user_id) as conn:
        run = _get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        job = _enqueue_job(conn, run, user_id, payload)
        _audit(
            conn,
            firm_id=str(run["firm_id"]),
            client_id=str(run["client_id"]),
            run_id=str(run["id"]),
            event_type="job.enqueued",
            actor_user_id=user_id,
            entity_type="job",
            entity_id=str(job["id"]),
            payload={"job_type": job["job_type"]},
        )
        return job


@router.post("/runs/{run_id}/reconcile/bank", response_model=EnqueueJobResponse, status_code=status.HTTP_201_CREATED)
def enqueue_reconcile_bank(run_id: UUID, payload: ReconcileBankRequest, user_id: str = Depends(get_user_id)) -> Any:
    request = EnqueueJobRequest(
        job_type="reconcile_bank",
        payload=payload.payload,
        idempotency_key=payload.idempotency_key or f"reconcile-bank-{run_id}",
    )
    with db_session(user_id) as conn:
        run = _get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        job = _enqueue_job(conn, run, user_id, request)
        _audit(
            conn,
            firm_id=str(run["firm_id"]),
            client_id=str(run["client_id"]),
            run_id=str(run["id"]),
            event_type="recon.bank.enqueued",
            actor_user_id=user_id,
            entity_type="job",
            entity_id=str(job["id"]),
        )
        return job


@router.get("/runs/{run_id}/summary")
def run_summary(run_id: UUID, user_id: str = Depends(get_user_id)) -> Any:
    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select r.id, r.status, r.firm_id, r.client_id,
                       s.expected_net_pay, s.matched_bank_total, s.delta,
                       s.status as tieout_status, s.computed_at
                from public.runs r
                left join public.run_bank_tieout_summaries s on s.run_id = r.id
                where r.id = %s
                """,
                (str(run_id),),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

            cur.execute(
                """
                select severity, count(*) as total
                from public.variances
                where run_id = %s and status = 'open'
                group by severity
                """,
                (str(run_id),),
            )
            severity_counts = {item["severity"]: item["total"] for item in cur.fetchall()}

        return {
            "run_id": str(row["id"]),
            "run_status": row["status"],
            "tieout": {
                "expected_net_pay": row["expected_net_pay"],
                "matched_bank_total": row["matched_bank_total"],
                "delta": row["delta"],
                "status": row["tieout_status"],
                "computed_at": row["computed_at"],
            },
            "open_variances": {
                "blocker": int(severity_counts.get("blocker", 0)),
                "review": int(severity_counts.get("review", 0)),
            },
        }


@router.get("/runs/{run_id}/bank-tieout")
def bank_tieout(run_id: UUID, user_id: str = Depends(get_user_id)) -> Any:
    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select r.id, r.client_id,
                       s.expected_net_pay, s.matched_bank_total, s.delta, s.status,
                       p.amount_tolerance, p.date_window_days, p.max_group_size,
                       p.enable_one_to_many, p.enable_many_to_one, p.require_allowed_account
                from public.runs r
                join public.client_recon_policies p on p.client_id = r.client_id
                left join public.run_bank_tieout_summaries s on s.run_id = r.id
                where r.id = %s
                """,
                (str(run_id),),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        return {
            "run_id": str(row["id"]),
            "expected_net_pay": row["expected_net_pay"],
            "matched_bank_total": row["matched_bank_total"],
            "delta": row["delta"],
            "status": row["status"],
            "policy": {
                "amount_tolerance": row["amount_tolerance"],
                "date_window_days": row["date_window_days"],
                "max_group_size": row["max_group_size"],
                "enable_one_to_many": row["enable_one_to_many"],
                "enable_many_to_one": row["enable_many_to_one"],
                "require_allowed_account": row["require_allowed_account"],
            },
        }


@router.get("/runs/{run_id}/variances")
def list_variances(
    run_id: UUID,
    category: str = "bank",
    variance_status: str = Query("open", alias="status"),
    user_id: str = Depends(get_user_id),
) -> Any:
    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute("select id from public.runs where id = %s", (str(run_id),))
            run = cur.fetchone()
            if not run:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

            cur.execute(
                """
                select id, code, severity, status, message, amount, event_date, account_ref, details
                from public.variances
                where run_id = %s and category = %s and status = %s::app.variance_status
                order by code, event_date nulls last, id
                """,
                (str(run_id), category, variance_status),
            )
            return [dict(item) for item in cur.fetchall()]


@router.get("/runs/{run_id}/match-groups")
def list_match_groups(run_id: UUID, user_id: str = Depends(get_user_id)) -> Any:
    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute("select id from public.runs where id = %s", (str(run_id),))
            run = cur.fetchone()
            if not run:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

            cur.execute(
                """
                select mg.id, mg.group_kind, mg.match_confidence,
                       mg.expected_total, mg.bank_total, mg.delta, mg.matched_on,
                       count(mgm.id)::int as members_count
                from public.match_groups mg
                left join public.match_group_members mgm on mgm.match_group_id = mg.id
                where mg.run_id = %s
                group by mg.id
                order by mg.group_kind, mg.id
                """,
                (str(run_id),),
            )
            return [dict(item) for item in cur.fetchall()]


@router.patch("/clients/{client_id}/recon-policy")
def update_recon_policy(client_id: UUID, payload: UpdateReconPolicyRequest, user_id: str = Depends(get_user_id)) -> Any:
    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update public.client_recon_policies
                set amount_tolerance = coalesce(%s, amount_tolerance),
                    date_window_days = coalesce(%s, date_window_days),
                    max_group_size = coalesce(%s, max_group_size),
                    enable_one_to_many = coalesce(%s, enable_one_to_many),
                    enable_many_to_one = coalesce(%s, enable_many_to_one),
                    require_allowed_account = coalesce(%s, require_allowed_account),
                    updated_at = now()
                where client_id = %s
                returning *
                """,
                (
                    payload.amount_tolerance,
                    payload.date_window_days,
                    payload.max_group_size,
                    payload.enable_one_to_many,
                    payload.enable_many_to_one,
                    payload.require_allowed_account,
                    str(client_id),
                ),
            )
            policy = cur.fetchone()
            if not policy:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client policy not found")
        return dict(policy)


@router.put("/clients/{client_id}/bank-accounts")
def replace_bank_accounts(client_id: UUID, payload: PutBankAccountsRequest, user_id: str = Depends(get_user_id)) -> Any:
    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute("select id, firm_id from public.clients where id = %s", (str(client_id),))
            client = cur.fetchone()
            if not client:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")

            cur.execute("delete from public.client_bank_accounts where client_id = %s", (str(client_id),))
            for account in payload.accounts:
                cur.execute(
                    """
                    insert into public.client_bank_accounts(client_id, firm_id, account_ref, label)
                    values (%s, %s, %s, %s)
                    """,
                    (str(client_id), client["firm_id"], account.account_ref, account.label),
                )
            cur.execute(
                """
                select id, account_ref, label
                from public.client_bank_accounts
                where client_id = %s
                order by account_ref
                """,
                (str(client_id),),
            )
            accounts = [dict(row) for row in cur.fetchall()]
        return {"client_id": str(client_id), "accounts": accounts}
