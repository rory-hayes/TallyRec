from __future__ import annotations

import json
from datetime import date
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from psycopg import Connection

from apps.api.app.db import db_session
from apps.api.app.schemas.api import (
    ApproveRunRequest,
    BatchRunResponse,
    CreateBatchRunsRequest,
    CreateClientRequest,
    CreateClientResponse,
    CreateFirmRequest,
    CreateFirmResponse,
    CreateRunRequest,
    CreateRunResponse,
    DashboardResponse,
    EnqueueExportPackRequest,
    EnqueueJobRequest,
    EnqueueJobResponse,
    PutBankAccountsRequest,
    ReadyForReviewRequest,
    ReconcileBankRequest,
    ReconcileGLRequest,
    RegisterSourceFileRequest,
    RegisterSourceFileResponse,
    ResolveVarianceRequest,
    UnlockRunRequest,
    UpdateReconPolicyRequest,
    UpdateUKTimingPolicyRequest,
    UpsertGLBucketAccountsRequest,
)
from libs.core.engine import add_business_days
from libs.core.utils import compute_import_health_score, get_storage_client, headers_hash, normalize_headers

router = APIRouter(prefix="/v1", tags=["v1"])

REVIEWER_ROLES = {"owner", "admin"}
PREPARER_ROLES = {"owner", "admin", "analyst"}
RESOLUTION_ACTIONS = {"matched", "explained", "expected_later", "ignored"}
REQUIRED_FILE_KINDS = {"bank", "payroll", "gl"}


def get_user_id(x_user_id: str | None = Header(default=None)) -> str:
    if not x_user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-User-Id header")
    return x_user_id


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


def _get_run(conn: Connection, run_id: UUID) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, firm_id, client_id, status, locked_at, locked_by, lock_reason
            from public.runs
            where id = %s
            """,
            (str(run_id),),
        )
        return cur.fetchone()


def _membership_role(conn: Connection, firm_id: str, user_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            select role
            from public.firm_memberships
            where firm_id = %s and user_id = %s::uuid
            """,
            (firm_id, user_id),
        )
        row = cur.fetchone()
        return row["role"] if row else None


def _ensure_run_unlocked(conn: Connection, run: dict[str, Any], user_id: str) -> None:
    if run["locked_at"] is None:
        return
    _audit(
        conn,
        firm_id=str(run["firm_id"]),
        client_id=str(run["client_id"]),
        run_id=str(run["id"]),
        event_type="run.edit_blocked",
        actor_user_id=user_id,
        entity_type="run",
        entity_id=str(run["id"]),
        payload={"reason": "Run is locked"},
    )
    # Persist the blocked-edit audit event before raising, otherwise outer
    # request rollback discards the event.
    if hasattr(conn, "commit"):
        conn.commit()
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Run is locked")


def _ensure_no_import_drift_blocker(conn: Connection, run_id: str) -> None:
    with conn.cursor() as cur:
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
        rows = cur.fetchall()
        row = rows[0] if rows else {}
        total = int(row.get("total", 0)) if isinstance(row, dict) else 0
        if total > 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Run has import drift blockers and cannot be reconciled until remapped",
            )


def _score_source_file_validation(validation_status: str) -> int:
    if validation_status == "validated":
        return 100
    if validation_status == "blocked_drift":
        return 0
    return 60


def _upsert_run_import_health_summary(
    conn: Connection,
    *,
    run_id: str,
    firm_id: str,
    client_id: str,
    actor_user_id: str,
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
            where run_id = %s and category = 'import' and code = 'IMP-001' and status = 'open'
            """,
            (run_id,),
        )
        drift_row = cur.fetchone() or {"total": 0}
        open_drift = int(drift_row.get("total", 0))

        cur.execute(
            """
            select
              count(*)::int as total_jobs,
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


def _overall_tie_status(
    *,
    blocker_count: int,
    review_count: int,
    bank_status: str | None,
    gl_status: str | None,
) -> str:
    if blocker_count > 0:
        return "Not tied"
    if review_count > 0:
        return "Needs review"
    if bank_status == "Tied" and gl_status == "Tied":
        return "Tied"
    return "Not tied"


def _run_due_bucket(must_close_by_date: date | None, today: date) -> str:
    if must_close_by_date is None:
        return "later"
    if must_close_by_date < today:
        return "overdue"
    if must_close_by_date == today:
        return "due_today"
    due_soon_cutoff = add_business_days(today, 2, "GB")
    if must_close_by_date <= due_soon_cutoff:
        return "due_soon"
    return "later"


def _enqueue_job(conn: Connection, run: dict[str, Any], user_id: str, payload: EnqueueJobRequest) -> dict[str, Any]:
    if payload.job_type in {"reconcile_bank", "reconcile_gl"}:
        _ensure_no_import_drift_blocker(conn, str(run["id"]))
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
        _ensure_run_unlocked(conn, run, user_id)
        observed_headers = normalize_headers(payload.observed_headers or [])
        observed_hash = headers_hash(observed_headers) if observed_headers else None
        validation_status = "pending"
        expected_headers: list[str] | None = None
        expected_hash: str | None = None

        with conn.cursor() as cur:
            if payload.mapping_template_id and observed_hash:
                cur.execute(
                    """
                    select expected_headers, expected_header_hash
                    from public.mapping_templates
                    where id = %s and firm_id = %s and file_kind = %s::app.file_kind
                    """,
                    (str(payload.mapping_template_id), str(run["firm_id"]), payload.file_kind),
                )
                template = cur.fetchone()
                if template:
                    if template["expected_headers"] is not None:
                        expected_headers = normalize_headers(list(template["expected_headers"]))
                    expected_hash = template["expected_header_hash"]
                    if expected_hash is None and expected_headers:
                        expected_hash = headers_hash(expected_headers)
                    if expected_hash is not None:
                        validation_status = "validated" if observed_hash == expected_hash else "blocked_drift"

            cur.execute(
                """
                insert into public.source_files(
                  run_id, firm_id, client_id, file_kind, filename, uri,
                  checksum_sha256, byte_size, mapping_template_id, registered_by,
                  observed_headers, observed_header_hash, import_validation_status, import_health_score
                )
                values (%s, %s, %s, %s::app.file_kind, %s, %s, %s, %s, %s, %s::uuid,
                        %s::jsonb, %s, %s, %s)
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
                    json.dumps(observed_headers) if observed_headers else None,
                    observed_hash,
                    validation_status,
                    _score_source_file_validation(validation_status),
                ),
            )
            source_file = cur.fetchone()

            if validation_status == "blocked_drift":
                drift_details = {
                    "source_file_id": str(source_file["id"]),
                    "mapping_template_id": str(payload.mapping_template_id) if payload.mapping_template_id else None,
                    "expected_headers": expected_headers or [],
                    "observed_headers": observed_headers,
                    "expected_header_hash": expected_hash,
                    "observed_header_hash": observed_hash,
                }
                cur.execute(
                    """
                    insert into public.variances(
                      run_id, firm_id, client_id, code, category, severity, status,
                      message, details
                    )
                    values (%s, %s, %s, 'IMP-001', 'import', 'blocker', 'open', %s, %s::jsonb)
                    """,
                    (
                        str(run_id),
                        str(run["firm_id"]),
                        str(run["client_id"]),
                        "Source file headers drifted from expected mapping template",
                        json.dumps(drift_details, sort_keys=True),
                    ),
                )

        if validation_status == "blocked_drift":
            _audit(
                conn,
                firm_id=str(run["firm_id"]),
                client_id=str(run["client_id"]),
                run_id=str(run_id),
                event_type="import.drift.detected",
                actor_user_id=user_id,
                entity_type="source_file",
                entity_id=str(source_file["id"]),
                payload={
                    "mapping_template_id": str(payload.mapping_template_id) if payload.mapping_template_id else None,
                    "expected_header_hash": expected_hash,
                    "observed_header_hash": observed_hash,
                },
            )

        _upsert_run_import_health_summary(
            conn,
            run_id=str(run_id),
            firm_id=str(run["firm_id"]),
            client_id=str(run["client_id"]),
            actor_user_id=user_id,
        )
        _audit(
            conn,
            firm_id=str(run["firm_id"]),
            client_id=str(run["client_id"]),
            run_id=str(run_id),
            event_type="source_file.registered",
            actor_user_id=user_id,
            entity_type="source_file",
            entity_id=str(source_file["id"]),
            payload={"import_validation_status": validation_status},
        )
        return source_file


@router.post("/runs/{run_id}/jobs", response_model=EnqueueJobResponse, status_code=status.HTTP_201_CREATED)
def enqueue_job(run_id: UUID, payload: EnqueueJobRequest, user_id: str = Depends(get_user_id)) -> Any:
    with db_session(user_id) as conn:
        run = _get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        if payload.job_type != "export_pack":
            _ensure_run_unlocked(conn, run, user_id)
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
    job_payload = dict(payload.payload)
    if payload.as_of_date:
        job_payload["as_of_date"] = payload.as_of_date.isoformat()
    request = EnqueueJobRequest(
        job_type="reconcile_bank",
        payload=job_payload,
        idempotency_key=payload.idempotency_key or f"reconcile-bank-{run_id}-{payload.as_of_date.isoformat() if payload.as_of_date else 'auto'}",
    )
    with db_session(user_id) as conn:
        run = _get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        _ensure_run_unlocked(conn, run, user_id)
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


@router.post("/runs/{run_id}/reconcile/gl", response_model=EnqueueJobResponse, status_code=status.HTTP_201_CREATED)
def enqueue_reconcile_gl(run_id: UUID, payload: ReconcileGLRequest, user_id: str = Depends(get_user_id)) -> Any:
    request = EnqueueJobRequest(
        job_type="reconcile_gl",
        payload=payload.payload,
        idempotency_key=payload.idempotency_key or f"reconcile-gl-{run_id}",
    )
    with db_session(user_id) as conn:
        run = _get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        _ensure_run_unlocked(conn, run, user_id)
        job = _enqueue_job(conn, run, user_id, request)
        _audit(
            conn,
            firm_id=str(run["firm_id"]),
            client_id=str(run["client_id"]),
            run_id=str(run["id"]),
            event_type="recon.gl.enqueued",
            actor_user_id=user_id,
            entity_type="job",
            entity_id=str(job["id"]),
        )
        return job


@router.post("/runs/{run_id}/export-pack", response_model=EnqueueJobResponse, status_code=status.HTTP_201_CREATED)
def enqueue_export_pack(run_id: UUID, payload: EnqueueExportPackRequest, user_id: str = Depends(get_user_id)) -> Any:
    request = EnqueueJobRequest(
        job_type="export_pack",
        payload={},
        idempotency_key=payload.idempotency_key or f"export-pack-{run_id}",
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
            event_type="export.pack.enqueued",
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
                select r.id, r.status as run_status, r.firm_id, r.client_id,
                       r.locked_at, r.locked_by, r.payday_date, r.must_close_by_date, r.sla_reminder_state,
                       b.expected_net_pay, b.matched_bank_total, b.delta,
                       b.status as bank_status, b.computed_at as bank_computed_at, b.policy_snapshot,
                       g.status as gl_status, g.computed_at as gl_computed_at,
                       a.status as approval_status, a.prepared_by, a.prepared_at, a.reviewer_id, a.reviewed_at,
                       h.health_score as import_health_score, h.health_band as import_health_band
                from public.runs r
                left join public.run_bank_tieout_summaries b on b.run_id = r.id
                left join public.run_gl_tieout_summaries g on g.run_id = r.id
                left join public.approvals a on a.run_id = r.id
                left join public.run_import_health_summaries h on h.run_id = r.id
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

        blockers = int(severity_counts.get("blocker", 0))
        reviews = int(severity_counts.get("review", 0))
        overall_tie = _overall_tie_status(
            blocker_count=blockers,
            review_count=reviews,
            bank_status=row["bank_status"],
            gl_status=row["gl_status"],
        )

        return {
            "run_id": str(row["id"]),
            "run_status": row["run_status"],
            "locked": row["locked_at"] is not None,
            "locked_at": row["locked_at"],
            "locked_by": row["locked_by"],
            "payday_date": row.get("payday_date"),
            "must_close_by_date": row.get("must_close_by_date"),
            "sla_reminder_state": row.get("sla_reminder_state"),
            "overall_tie_status": overall_tie,
            "tieout": {
                "expected_net_pay": row["expected_net_pay"],
                "matched_bank_total": row["matched_bank_total"],
                "delta": row["delta"],
                "status": row["bank_status"],
                "computed_at": row["bank_computed_at"],
                "policy_snapshot": row.get("policy_snapshot") or {},
            },
            "gl_tieout": {
                "status": row["gl_status"],
                "computed_at": row["gl_computed_at"],
            },
            "import_health": {
                "score": row.get("import_health_score"),
                "band": row.get("import_health_band"),
            },
            "approval": {
                "status": row["approval_status"],
                "prepared_by": row["prepared_by"],
                "prepared_at": row["prepared_at"],
                "reviewer_id": row["reviewer_id"],
                "reviewed_at": row["reviewed_at"],
            },
            "open_variances": {
                "blocker": blockers,
                "review": reviews,
            },
        }


@router.get("/runs/{run_id}/bank-tieout")
def bank_tieout(run_id: UUID, user_id: str = Depends(get_user_id)) -> Any:
    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select r.id, r.client_id,
                       s.expected_net_pay, s.matched_bank_total, s.delta, s.status, s.policy_snapshot,
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
            "timing": (row["policy_snapshot"] or {}).get("timing"),
        }


@router.get("/runs/{run_id}/gl-tieout")
def gl_tieout(run_id: UUID, user_id: str = Depends(get_user_id)) -> Any:
    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select run_id, payroll_totals, gl_totals, deltas,
                       is_balanced, status, rules_used, computed_at
                from public.run_gl_tieout_summaries
                where run_id = %s
                """,
                (str(run_id),),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="GL tie-out not found")
            return dict(row)


@router.get("/runs/{run_id}/variances")
def list_variances(
    run_id: UUID,
    category: str | None = None,
    variance_status: str = Query("open", alias="status"),
    code: str | None = None,
    user_id: str = Depends(get_user_id),
) -> Any:
    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute("select id from public.runs where id = %s", (str(run_id),))
            run = cur.fetchone()
            if not run:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

            filters = ["run_id = %s", "status = %s::app.variance_status"]
            values: list[Any] = [str(run_id), variance_status]
            if category:
                filters.append("category = %s")
                values.append(category)
            if code:
                filters.append("code = %s")
                values.append(code)

            cur.execute(
                f"""
                select id, code, category, severity, status, message, amount,
                       event_date, account_ref, details, note,
                       changed_by, changed_at, resolution_action,
                       ignored_needs_reviewer_approval, ignored_approved_by, ignored_approved_at
                from public.variances
                where {' and '.join(filters)}
                order by code, event_date nulls last, id
                """,
                tuple(values),
            )
            return [dict(item) for item in cur.fetchall()]


@router.get("/runs/{run_id}/variances/{variance_id}")
def get_variance_detail(run_id: UUID, variance_id: UUID, user_id: str = Depends(get_user_id)) -> Any:
    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select v.*
                from public.variances v
                where v.run_id = %s and v.id = %s
                """,
                (str(run_id), str(variance_id)),
            )
            variance = cur.fetchone()
            if not variance:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variance not found")

            cur.execute(
                """
                select id, action, note, actor_user_id, created_at
                from public.variance_resolution_events
                where variance_id = %s
                order by created_at desc, id desc
                limit 20
                """,
                (str(variance_id),),
            )
            events = [dict(row) for row in cur.fetchall()]
            result = dict(variance)
            result["events"] = events
            return result


@router.post("/variances/{variance_id}/resolve")
def resolve_variance(variance_id: UUID, payload: ResolveVarianceRequest, user_id: str = Depends(get_user_id)) -> Any:
    action = payload.action.lower().strip()
    if action not in RESOLUTION_ACTIONS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unsupported resolution action")
    if not payload.note.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Resolution note is required")

    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select v.id, v.run_id, v.firm_id, v.client_id,
                       r.locked_at
                from public.variances v
                join public.runs r on r.id = v.run_id
                where v.id = %s
                """,
                (str(variance_id),),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variance not found")

            run = {
                "id": row["run_id"],
                "firm_id": row["firm_id"],
                "client_id": row["client_id"],
                "locked_at": row["locked_at"],
            }
            _ensure_run_unlocked(conn, run, user_id)

            variance_status = "ignored" if action == "ignored" else "resolved"
            needs_reviewer = action == "ignored"

            cur.execute(
                """
                update public.variances
                set resolution_action = %s::app.variance_resolution_action,
                    status = %s::app.variance_status,
                    note = %s,
                    changed_by = %s::uuid,
                    changed_at = now(),
                    ignored_needs_reviewer_approval = %s,
                    ignored_approved_by = null,
                    ignored_approved_at = null
                where id = %s
                returning *
                """,
                (action, variance_status, payload.note, user_id, needs_reviewer, str(variance_id)),
            )
            updated = cur.fetchone()

            cur.execute(
                """
                insert into public.variance_resolution_events(
                  variance_id, run_id, firm_id, client_id,
                  action, note, actor_user_id
                )
                values (%s, %s, %s, %s, %s, %s, %s::uuid)
                """,
                (
                    str(variance_id),
                    str(updated["run_id"]),
                    str(updated["firm_id"]),
                    str(updated["client_id"]),
                    action,
                    payload.note,
                    user_id,
                ),
            )

        _audit(
            conn,
            firm_id=str(updated["firm_id"]),
            client_id=str(updated["client_id"]),
            run_id=str(updated["run_id"]),
            event_type="variance.resolved",
            actor_user_id=user_id,
            entity_type="variance",
            entity_id=str(variance_id),
            payload={"action": action, "status": variance_status, "note": payload.note},
        )
        return dict(updated)


@router.post("/variances/{variance_id}/approve-ignored")
def approve_ignored_variance(variance_id: UUID, user_id: str = Depends(get_user_id)) -> Any:
    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select v.*, r.locked_at
                from public.variances v
                join public.runs r on r.id = v.run_id
                where v.id = %s
                """,
                (str(variance_id),),
            )
            variance = cur.fetchone()
            if not variance:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variance not found")

            run = {
                "id": variance["run_id"],
                "firm_id": variance["firm_id"],
                "client_id": variance["client_id"],
                "locked_at": variance["locked_at"],
            }
            _ensure_run_unlocked(conn, run, user_id)

            role = _membership_role(conn, str(variance["firm_id"]), user_id)
            if role not in REVIEWER_ROLES:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Reviewer role required")

            if variance["resolution_action"] != "ignored" or not variance["ignored_needs_reviewer_approval"]:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Variance does not require reviewer ignore approval")

            cur.execute(
                """
                update public.variances
                set ignored_needs_reviewer_approval = false,
                    ignored_approved_by = %s::uuid,
                    ignored_approved_at = now(),
                    changed_by = %s::uuid,
                    changed_at = now()
                where id = %s
                returning *
                """,
                (user_id, user_id, str(variance_id)),
            )
            updated = cur.fetchone()

            cur.execute(
                """
                insert into public.variance_resolution_events(
                  variance_id, run_id, firm_id, client_id,
                  action, note, actor_user_id
                )
                values (%s, %s, %s, %s, %s, %s, %s::uuid)
                """,
                (
                    str(variance_id),
                    str(updated["run_id"]),
                    str(updated["firm_id"]),
                    str(updated["client_id"]),
                    "ignored_reviewer_approved",
                    "Reviewer approved ignored variance",
                    user_id,
                ),
            )

        _audit(
            conn,
            firm_id=str(updated["firm_id"]),
            client_id=str(updated["client_id"]),
            run_id=str(updated["run_id"]),
            event_type="variance.ignore.approved",
            actor_user_id=user_id,
            entity_type="variance",
            entity_id=str(variance_id),
            payload={"approved": True},
        )
        return dict(updated)


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


@router.patch("/clients/{client_id}/uk-timing-policy")
def update_uk_timing_policy(
    client_id: UUID,
    payload: UpdateUKTimingPolicyRequest,
    user_id: str = Depends(get_user_id),
) -> Any:
    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute("select id, firm_id from public.clients where id = %s", (str(client_id),))
            client = cur.fetchone()
            if not client:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
            cur.execute(
                """
                insert into public.client_uk_timing_policies(
                  client_id, firm_id, tax_due_day, pension_due_day,
                  bacs_visibility_business_days, holiday_calendar, enabled, updated_at
                )
                values (%s, %s, coalesce(%s, 22), coalesce(%s, 22), coalesce(%s, 3), coalesce(%s, 'GB'), coalesce(%s, true), now())
                on conflict (client_id)
                do update set tax_due_day = coalesce(excluded.tax_due_day, public.client_uk_timing_policies.tax_due_day),
                              pension_due_day = coalesce(excluded.pension_due_day, public.client_uk_timing_policies.pension_due_day),
                              bacs_visibility_business_days = coalesce(excluded.bacs_visibility_business_days, public.client_uk_timing_policies.bacs_visibility_business_days),
                              holiday_calendar = coalesce(excluded.holiday_calendar, public.client_uk_timing_policies.holiday_calendar),
                              enabled = coalesce(excluded.enabled, public.client_uk_timing_policies.enabled),
                              updated_at = excluded.updated_at
                returning *
                """,
                (
                    str(client_id),
                    str(client["firm_id"]),
                    payload.tax_due_day,
                    payload.pension_due_day,
                    payload.bacs_visibility_business_days,
                    payload.holiday_calendar,
                    payload.enabled,
                ),
            )
            row = cur.fetchone()
        return dict(row)


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


@router.put("/clients/{client_id}/gl-bucket-accounts")
def upsert_gl_bucket_accounts(client_id: UUID, payload: UpsertGLBucketAccountsRequest, user_id: str = Depends(get_user_id)) -> Any:
    bucket_map = {
        "net_pay_control": sorted(set(payload.net_pay_control)),
        "taxes": sorted(set(payload.taxes)),
        "pension": sorted(set(payload.pension)),
        "other": sorted(set(payload.other)),
    }
    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute("select id, firm_id from public.clients where id = %s", (str(client_id),))
            client = cur.fetchone()
            if not client:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")

            cur.execute("delete from public.client_gl_bucket_accounts where client_id = %s", (str(client_id),))
            for bucket, accounts in bucket_map.items():
                for account_code in accounts:
                    cur.execute(
                        """
                        insert into public.client_gl_bucket_accounts(client_id, firm_id, bucket, account_code)
                        values (%s, %s, %s::app.gl_bucket, %s)
                        """,
                        (str(client_id), client["firm_id"], bucket, account_code),
                    )

            cur.execute(
                """
                select bucket, account_code
                from public.client_gl_bucket_accounts
                where client_id = %s
                order by bucket, account_code
                """,
                (str(client_id),),
            )
            accounts = [dict(row) for row in cur.fetchall()]
        return {"client_id": str(client_id), "buckets": accounts}


@router.post("/runs/{run_id}/ready-for-review")
def ready_for_review(run_id: UUID, payload: ReadyForReviewRequest, user_id: str = Depends(get_user_id)) -> Any:
    with db_session(user_id) as conn:
        run = _get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        _ensure_run_unlocked(conn, run, user_id)

        role = _membership_role(conn, str(run["firm_id"]), user_id)
        if role not in PREPARER_ROLES:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Preparer role required")

        with conn.cursor() as cur:
            cur.execute(
                """
                insert into public.approvals(
                  run_id, firm_id, client_id, status,
                  prepared_by, prepared_at, notes
                )
                values (%s, %s, %s, 'pending', %s::uuid, now(), %s)
                on conflict (run_id)
                do update set status = 'pending',
                              prepared_by = excluded.prepared_by,
                              prepared_at = excluded.prepared_at,
                              reviewer_id = null,
                              reviewed_at = null,
                              notes = excluded.notes
                returning *
                """,
                (str(run_id), str(run["firm_id"]), str(run["client_id"]), user_id, payload.note),
            )
            approval = cur.fetchone()
            cur.execute("update public.runs set status = 'ready_for_review', updated_at = now() where id = %s", (str(run_id),))

        _audit(
            conn,
            firm_id=str(run["firm_id"]),
            client_id=str(run["client_id"]),
            run_id=str(run_id),
            event_type="run.ready_for_review",
            actor_user_id=user_id,
            entity_type="approval",
            entity_id=str(approval["id"]),
            payload={"note": payload.note},
        )
        return {"run_id": str(run_id), "status": "ready_for_review", "approval": dict(approval)}


@router.post("/runs/{run_id}/approve")
def approve_run(run_id: UUID, payload: ApproveRunRequest, user_id: str = Depends(get_user_id)) -> Any:
    with db_session(user_id) as conn:
        run = _get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

        role = _membership_role(conn, str(run["firm_id"]), user_id)
        if role not in REVIEWER_ROLES:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Reviewer role required")

        with conn.cursor() as cur:
            cur.execute("select * from public.approvals where run_id = %s", (str(run_id),))
            approval = cur.fetchone()
            if not approval:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Run is not ready for review")
            if approval["prepared_by"] and str(approval["prepared_by"]) == user_id:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Preparer cannot self-approve")

            cur.execute(
                """
                select
                  count(*) filter (where severity = 'blocker' and status = 'open')::int as blocker_open,
                  count(*) filter (where ignored_needs_reviewer_approval = true)::int as ignored_pending
                from public.variances
                where run_id = %s
                """,
                (str(run_id),),
            )
            counts = cur.fetchone()
            if counts["blocker_open"] > 0:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot approve run with open blocker variances")
            if counts["ignored_pending"] > 0:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ignored variances require reviewer approval")

            cur.execute(
                """
                update public.approvals
                set status = 'approved',
                    reviewer_id = %s::uuid,
                    reviewed_at = now(),
                    notes = coalesce(%s, notes)
                where run_id = %s
                returning *
                """,
                (user_id, payload.note, str(run_id)),
            )
            updated = cur.fetchone()

            cur.execute(
                """
                update public.runs
                set status = 'approved',
                    locked_at = now(),
                    locked_by = %s::uuid,
                    lock_reason = 'reviewer_approval',
                    updated_at = now()
                where id = %s
                returning id, status, locked_at, locked_by, lock_reason
                """,
                (user_id, str(run_id)),
            )
            run_row = cur.fetchone()

        _audit(
            conn,
            firm_id=str(run["firm_id"]),
            client_id=str(run["client_id"]),
            run_id=str(run_id),
            event_type="run.approved",
            actor_user_id=user_id,
            entity_type="approval",
            entity_id=str(updated["id"]),
            payload={"note": payload.note},
        )
        _audit(
            conn,
            firm_id=str(run["firm_id"]),
            client_id=str(run["client_id"]),
            run_id=str(run_id),
            event_type="run.locked",
            actor_user_id=user_id,
            entity_type="run",
            entity_id=str(run_id),
            payload={"reason": "reviewer_approval"},
        )
        return {"run": dict(run_row), "approval": dict(updated)}


@router.post("/runs/{run_id}/unlock")
def unlock_run(run_id: UUID, payload: UnlockRunRequest, user_id: str = Depends(get_user_id)) -> Any:
    with db_session(user_id) as conn:
        run = _get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

        role = _membership_role(conn, str(run["firm_id"]), user_id)
        if role not in REVIEWER_ROLES:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Reviewer role required")

        with conn.cursor() as cur:
            cur.execute(
                """
                update public.runs
                set locked_at = null,
                    locked_by = null,
                    lock_reason = %s,
                    status = 'completed',
                    updated_at = now()
                where id = %s
                returning id, status, locked_at, lock_reason
                """,
                (payload.reason, str(run_id)),
            )
            updated = cur.fetchone()

        _audit(
            conn,
            firm_id=str(run["firm_id"]),
            client_id=str(run["client_id"]),
            run_id=str(run_id),
            event_type="run.unlocked",
            actor_user_id=user_id,
            entity_type="run",
            entity_id=str(run_id),
            payload={"reason": payload.reason},
        )
        return dict(updated)


def _refresh_batch_status(conn: Connection, batch_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select bi.id, bi.client_id, bi.run_id, bi.status, bi.bank_job_id, bi.gl_job_id, bi.error, bi.updated_at,
                   bj.status as bank_job_status, gj.status as gl_job_status
            from public.run_batch_items bi
            left join public.jobs bj on bj.id = bi.bank_job_id
            left join public.jobs gj on gj.id = bi.gl_job_id
            where bi.batch_id = %s
            order by bi.client_id, bi.id
            """,
            (batch_id,),
        )
        items = [dict(row) for row in cur.fetchall()]

        succeeded = 0
        failed = 0
        for item in items:
            bank_status = item.get("bank_job_status")
            gl_status = item.get("gl_job_status")
            derived_status = item["status"]
            if bank_status == "failed" or gl_status == "failed":
                derived_status = "failed"
            elif bank_status == "succeeded" and gl_status == "succeeded":
                derived_status = "succeeded"
            elif bank_status == "running" or gl_status == "running":
                derived_status = "running"
            else:
                derived_status = "queued"

            if derived_status != item["status"]:
                cur.execute(
                    "update public.run_batch_items set status = %s::app.batch_status, updated_at = now() where id = %s",
                    (derived_status, str(item["id"])),
                )
                item["status"] = derived_status
            if item["status"] == "succeeded":
                succeeded += 1
            elif item["status"] == "failed":
                failed += 1

        total = len(items)
        queued = sum(1 for item in items if item["status"] == "queued")
        running = sum(1 for item in items if item["status"] == "running")
        if total == 0:
            batch_status = "queued"
        elif succeeded == total:
            batch_status = "succeeded"
        elif failed == total:
            batch_status = "failed"
        elif failed > 0 and succeeded > 0 and (failed + succeeded) == total:
            batch_status = "partially_failed"
        elif running > 0:
            batch_status = "running"
        elif queued > 0 and failed > 0 and succeeded > 0:
            batch_status = "partially_failed"
        elif queued > 0:
            batch_status = "queued"
        elif failed > 0 and succeeded > 0:
            batch_status = "partially_failed"
        elif failed > 0:
            batch_status = "failed"
        else:
            batch_status = "queued"

        cur.execute(
            """
            update public.run_batches
            set status = %s::app.batch_status,
                succeeded_runs = %s,
                failed_runs = %s,
                updated_at = now()
            where id = %s
            returning *
            """,
            (batch_status, succeeded, failed, batch_id),
        )
        batch = cur.fetchone()
    return batch, items


def _batch_response_payload(batch_row: dict[str, Any], item_rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": batch_row["id"],
        "firm_id": batch_row["firm_id"],
        "period_start": batch_row["period_start"],
        "period_end": batch_row["period_end"],
        "status": batch_row["status"],
        "requested_clients": batch_row["requested_clients"],
        "created_runs": batch_row["created_runs"],
        "queued_jobs": batch_row["queued_jobs"],
        "succeeded_runs": batch_row["succeeded_runs"],
        "failed_runs": batch_row["failed_runs"],
        "options": batch_row.get("options") or {},
        "created_at": batch_row["created_at"],
        "updated_at": batch_row["updated_at"],
        "items": [
            {
                "id": item["id"],
                "client_id": item["client_id"],
                "run_id": item["run_id"],
                "status": item["status"],
                "bank_job_id": item["bank_job_id"],
                "gl_job_id": item["gl_job_id"],
                "error": item["error"] or {},
                "updated_at": item["updated_at"],
            }
            for item in item_rows
        ],
    }


@router.post("/batches/runs", response_model=BatchRunResponse, status_code=status.HTTP_201_CREATED)
def create_batch_runs(payload: CreateBatchRunsRequest, user_id: str = Depends(get_user_id)) -> Any:
    unique_clients = sorted({str(client_id) for client_id in payload.client_ids})
    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into public.run_batches(
                  firm_id, created_by, period_start, period_end, status,
                  requested_clients, options
                )
                values (%s, %s::uuid, %s, %s, 'running', %s, %s::jsonb)
                returning *
                """,
                (
                    str(payload.firm_id),
                    user_id,
                    payload.period_start,
                    payload.period_end,
                    len(unique_clients),
                    json.dumps(
                        {"as_of_date": payload.as_of_date.isoformat() if payload.as_of_date else None},
                        sort_keys=True,
                    ),
                ),
            )
            batch = cur.fetchone()

        created_runs = 0
        queued_jobs = 0
        succeeded_items = 0
        failed_items = 0
        for client_id in unique_clients:
            item_error: dict[str, Any] = {}
            item_status = "queued"
            run_id: str | None = None
            bank_job_id: str | None = None
            gl_job_id: str | None = None
            with conn.cursor() as cur:
                cur.execute("savepoint batch_item_sp")
                try:
                    cur.execute(
                        "select id from public.clients where id = %s and firm_id = %s",
                        (client_id, str(payload.firm_id)),
                    )
                    client = cur.fetchone()
                    if not client:
                        raise RuntimeError("Client not found for firm")

                    cur.execute(
                        """
                        select id, firm_id, client_id, status, locked_at, locked_by, lock_reason
                        from public.runs
                        where firm_id = %s and client_id = %s and period_start = %s and period_end = %s
                        order by created_at desc, id desc
                        limit 1
                        """,
                        (str(payload.firm_id), client_id, payload.period_start, payload.period_end),
                    )
                    run = cur.fetchone()
                    if run:
                        run_id = str(run["id"])
                    else:
                        cur.execute(
                            """
                            insert into public.runs(firm_id, client_id, period_start, period_end, created_by)
                            values (%s, %s, %s, %s, %s::uuid)
                            returning id, firm_id, client_id, status, locked_at, locked_by, lock_reason
                            """,
                            (str(payload.firm_id), client_id, payload.period_start, payload.period_end, user_id),
                        )
                        run = cur.fetchone()
                        run_id = str(run["id"])
                        created_runs += 1

                    _ensure_no_import_drift_blocker(conn, run_id)

                    bank_payload = {"batch_id": str(batch["id"])}
                    if payload.as_of_date:
                        bank_payload["as_of_date"] = payload.as_of_date.isoformat()
                    bank_job = _enqueue_job(
                        conn,
                        run,
                        user_id,
                        EnqueueJobRequest(
                            job_type="reconcile_bank",
                            payload=bank_payload,
                            idempotency_key=f"batch-{batch['id']}-client-{client_id}-bank",
                        ),
                    )
                    gl_job = _enqueue_job(
                        conn,
                        run,
                        user_id,
                        EnqueueJobRequest(
                            job_type="reconcile_gl",
                            payload={"batch_id": str(batch["id"])},
                            idempotency_key=f"batch-{batch['id']}-client-{client_id}-gl",
                        ),
                    )
                    bank_job_id = str(bank_job["id"])
                    gl_job_id = str(gl_job["id"])
                    queued_jobs += 2
                    item_status = "queued"
                    succeeded_items += 1
                except Exception as exc:
                    cur.execute("rollback to savepoint batch_item_sp")
                    item_status = "failed"
                    failed_items += 1
                    item_error = {"message": str(exc), "type": type(exc).__name__}
                    _audit(
                        conn,
                        firm_id=str(payload.firm_id),
                        client_id=client_id,
                        run_id=run_id,
                        event_type="batch.item.failed",
                        actor_user_id=user_id,
                        entity_type="run_batch",
                        entity_id=str(batch["id"]),
                        payload={"client_id": client_id, "error": item_error},
                    )
                finally:
                    cur.execute("release savepoint batch_item_sp")

                cur.execute(
                    """
                    insert into public.run_batch_items(
                      batch_id, firm_id, client_id, run_id, status,
                      bank_job_id, gl_job_id, error, updated_at
                    )
                    values (%s, %s, %s, %s, %s::app.batch_status, %s, %s, %s::jsonb, now())
                    returning id
                    """,
                    (
                        str(batch["id"]),
                        str(payload.firm_id),
                        client_id,
                        run_id,
                        item_status,
                        bank_job_id,
                        gl_job_id,
                        json.dumps(item_error, sort_keys=True),
                    ),
                )
                item_id = cur.fetchone()["id"]
                if item_status != "failed":
                    _audit(
                        conn,
                        firm_id=str(payload.firm_id),
                        client_id=client_id,
                        run_id=run_id,
                        event_type="batch.item.queued",
                        actor_user_id=user_id,
                        entity_type="run_batch_item",
                        entity_id=str(item_id),
                        payload={"run_id": run_id, "bank_job_id": bank_job_id, "gl_job_id": gl_job_id},
                    )

        total_items = len(unique_clients)
        if failed_items == 0:
            batch_status = "succeeded" if total_items > 0 else "queued"
        elif failed_items == total_items:
            batch_status = "failed"
        else:
            batch_status = "partially_failed"

        with conn.cursor() as cur:
            cur.execute(
                """
                update public.run_batches
                set status = %s::app.batch_status,
                    created_runs = %s,
                    queued_jobs = %s,
                    succeeded_runs = %s,
                    failed_runs = %s,
                    updated_at = now()
                where id = %s
                returning *
                """,
                (
                    batch_status,
                    created_runs,
                    queued_jobs,
                    succeeded_items,
                    failed_items,
                    str(batch["id"]),
                ),
            )
            batch = cur.fetchone()

        _audit(
            conn,
            firm_id=str(payload.firm_id),
            client_id=None,
            run_id=None,
            event_type="batch.created",
            actor_user_id=user_id,
            entity_type="run_batch",
            entity_id=str(batch["id"]),
            payload={
                "requested_clients": len(unique_clients),
                "created_runs": created_runs,
                "queued_jobs": queued_jobs,
                "failed_runs": failed_items,
            },
        )
        _audit(
            conn,
            firm_id=str(payload.firm_id),
            client_id=None,
            run_id=None,
            event_type="batch.completed",
            actor_user_id=user_id,
            entity_type="run_batch",
            entity_id=str(batch["id"]),
            payload={"status": batch_status},
        )

        batch_row, item_rows = _refresh_batch_status(conn, str(batch["id"]))
        return _batch_response_payload(dict(batch_row), item_rows)


@router.get("/batches/{batch_id}", response_model=BatchRunResponse)
def get_batch_runs(batch_id: UUID, user_id: str = Depends(get_user_id)) -> Any:
    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute("select * from public.run_batches where id = %s", (str(batch_id),))
            batch = cur.fetchone()
            if not batch:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")

        batch_row, item_rows = _refresh_batch_status(conn, str(batch_id))
        return _batch_response_payload(dict(batch_row), item_rows)


@router.get("/dashboard", response_model=DashboardResponse)
def bureau_dashboard(
    firm_id: UUID,
    status_filter: str | None = Query(default=None, alias="status"),
    due_before: date | None = None,
    needs_attention: bool = False,
    client_id: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user_id: str = Depends(get_user_id),
) -> Any:
    today = date.today()
    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select r.id as run_id, r.client_id, c.name as client_name, r.status as run_status,
                       r.payday_date, r.must_close_by_date, r.sla_reminder_state,
                       b.status as bank_status, g.status as gl_status,
                       coalesce(v.blockers, 0)::int as open_blockers,
                       coalesce(v.reviews, 0)::int as open_review,
                       h.health_band as import_health_band,
                       j.status as latest_job_status
                from public.runs r
                join public.clients c on c.id = r.client_id
                left join public.run_bank_tieout_summaries b on b.run_id = r.id
                left join public.run_gl_tieout_summaries g on g.run_id = r.id
                left join public.run_import_health_summaries h on h.run_id = r.id
                left join lateral (
                  select
                    count(*) filter (where severity = 'blocker' and status = 'open') as blockers,
                    count(*) filter (where severity = 'review' and status = 'open') as reviews
                  from public.variances vv
                  where vv.run_id = r.id
                ) v on true
                left join lateral (
                  select status
                  from public.jobs jj
                  where jj.run_id = r.id
                  order by queued_at desc, id desc
                  limit 1
                ) j on true
                where r.firm_id = %s
                order by r.must_close_by_date nulls last, r.status, r.id
                """,
                (str(firm_id),),
            )
            rows = [dict(row) for row in cur.fetchall()]

    filtered: list[dict[str, Any]] = []
    for row in rows:
        overall_tie = _overall_tie_status(
            blocker_count=int(row["open_blockers"]),
            review_count=int(row["open_review"]),
            bank_status=row["bank_status"],
            gl_status=row["gl_status"],
        )
        due_bucket = _run_due_bucket(row["must_close_by_date"], today)
        attention = (
            int(row["open_blockers"]) > 0
            or row["run_status"] == "failed"
            or (due_bucket in {"overdue", "due_today"} and row["run_status"] != "approved")
        )
        if status_filter and row["run_status"] != status_filter:
            continue
        if due_before and row["must_close_by_date"] and row["must_close_by_date"] > due_before:
            continue
        if client_id and str(row["client_id"]) != str(client_id):
            continue
        if needs_attention and not attention:
            continue
        row["overall_tie_status"] = overall_tie
        row["due_bucket"] = due_bucket
        row["needs_attention"] = attention
        filtered.append(row)

    counts_by_status: dict[str, int] = {}
    counts_by_due_bucket = {"overdue": 0, "due_today": 0, "due_soon": 0, "later": 0}
    for row in filtered:
        counts_by_status[row["run_status"]] = counts_by_status.get(row["run_status"], 0) + 1
        counts_by_due_bucket[row["due_bucket"]] += 1

    window = filtered[offset : offset + limit]
    return {
        "counts_by_status": counts_by_status,
        "counts_by_due_bucket": counts_by_due_bucket,
        "runs": [
            {
                "run_id": item["run_id"],
                "client_id": item["client_id"],
                "client_name": item["client_name"],
                "run_status": item["run_status"],
                "overall_tie_status": item["overall_tie_status"],
                "open_blockers": int(item["open_blockers"]),
                "open_review": int(item["open_review"]),
                "payday_date": item["payday_date"],
                "must_close_by_date": item["must_close_by_date"],
                "import_health_band": item["import_health_band"],
                "latest_job_status": item["latest_job_status"],
                "sla_reminder_state": item["sla_reminder_state"],
            }
            for item in window
        ],
        "total": len(filtered),
    }


@router.get("/runs/{run_id}/export-packs")
def list_export_packs(run_id: UUID, user_id: str = Depends(get_user_id)) -> Any:
    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute("select id from public.runs where id = %s", (str(run_id),))
            run = cur.fetchone()
            if not run:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

            cur.execute(
                """
                select id, status, pack_hash, storage_bucket, storage_path,
                       generated_at, error, created_at, updated_at
                from public.export_packs
                where run_id = %s
                order by generated_at desc nulls last, created_at desc
                """,
                (str(run_id),),
            )
            return [dict(row) for row in cur.fetchall()]


@router.get("/export-packs/{export_pack_id}/download")
def download_export_pack(export_pack_id: UUID, user_id: str = Depends(get_user_id)) -> Response:
    with db_session(user_id) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select ep.id, ep.storage_bucket, ep.storage_path
                from public.export_packs ep
                join public.runs r on r.id = ep.run_id
                where ep.id = %s
                """,
                (str(export_pack_id),),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export pack not found")
            if not row["storage_bucket"] or not row["storage_path"]:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Export pack is not available for download")

    client = get_storage_client()
    try:
        payload = client.download_bytes(row["storage_bucket"], row["storage_path"])
    except Exception as exc:  # pragma: no cover - storage backend dependent
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Storage download failed: {exc}") from exc

    filename = f"audit-pack-{export_pack_id}.zip"
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
