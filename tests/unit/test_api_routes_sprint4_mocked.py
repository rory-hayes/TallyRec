from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

import apps.api.app.api.routes as routes
from apps.api.app.schemas.api import CreateBatchRunsRequest, ReconcileBankRequest, RegisterSourceFileRequest, UpdateUKTimingPolicyRequest


class FakeCursor:
    def __init__(self, conn: "FakeConn") -> None:
        self.conn = conn

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, query: str, params: tuple[Any, ...] | None = None) -> None:
        self.conn.executed.append((" ".join(query.lower().split()), params or tuple()))

    def fetchone(self) -> Any:
        if self.conn.ones:
            return self.conn.ones.popleft()
        return None

    def fetchall(self) -> list[Any]:
        if self.conn.alls:
            return self.conn.alls.popleft()
        return []


class FakeConn:
    def __init__(self, *, ones: list[Any] | None = None, alls: list[list[Any]] | None = None) -> None:
        self.ones = deque(ones or [])
        self.alls = deque(alls or [])
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)


@pytest.fixture()
def push_conn(monkeypatch: pytest.MonkeyPatch):
    conn_queue: deque[FakeConn] = deque()

    @contextmanager
    def fake_db_session(_user_id: str):
        if not conn_queue:
            raise AssertionError("No fake connection queued")
        yield conn_queue.popleft()

    monkeypatch.setattr(routes, "db_session", fake_db_session)
    yield conn_queue.append


def test_sprint4_helpers_due_bucket_and_tie_status() -> None:
    today = date(2025, 2, 28)
    assert routes._overall_tie_status(blocker_count=1, review_count=0, bank_status="Tied", gl_status="Tied") == "Not tied"
    assert routes._overall_tie_status(blocker_count=0, review_count=1, bank_status="Tied", gl_status="Tied") == "Needs review"
    assert routes._overall_tie_status(blocker_count=0, review_count=0, bank_status="Tied", gl_status="Tied") == "Tied"
    assert routes._overall_tie_status(blocker_count=0, review_count=0, bank_status="Not tied", gl_status="Tied") == "Not tied"

    assert routes._run_due_bucket(None, today) == "later"
    assert routes._run_due_bucket(today - timedelta(days=1), today) == "overdue"
    assert routes._run_due_bucket(today, today) == "due_today"
    assert routes._run_due_bucket(today + timedelta(days=1), today) == "due_soon"
    assert routes._run_due_bucket(today + timedelta(days=30), today) == "later"


def test_update_uk_timing_policy_paths(push_conn) -> None:
    user_id = "00000000-0000-0000-0000-00000000b001"
    client_id = uuid4()
    firm_id = uuid4()
    now = datetime.now(timezone.utc)

    push_conn(FakeConn(ones=[None]))
    with pytest.raises(HTTPException) as err:
        routes.update_uk_timing_policy(client_id, UpdateUKTimingPolicyRequest(enabled=True), user_id=user_id)
    assert err.value.status_code == 404

    push_conn(
        FakeConn(
            ones=[
                {"id": str(client_id), "firm_id": str(firm_id)},
                {
                    "client_id": str(client_id),
                    "firm_id": str(firm_id),
                    "tax_due_day": 20,
                    "pension_due_day": 21,
                    "bacs_visibility_business_days": 2,
                    "holiday_calendar": "GB",
                    "enabled": False,
                    "updated_at": now,
                },
            ]
        )
    )
    updated = routes.update_uk_timing_policy(
        client_id,
        UpdateUKTimingPolicyRequest(
            tax_due_day=20,
            pension_due_day=21,
            bacs_visibility_business_days=2,
            holiday_calendar="GB",
            enabled=False,
        ),
        user_id=user_id,
    )
    assert updated["tax_due_day"] == 20
    assert updated["enabled"] is False


def test_register_source_file_drift_and_enqueue_blocked(push_conn, monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = "00000000-0000-0000-0000-00000000b002"
    run_id = uuid4()
    firm_id = uuid4()
    client_id = uuid4()
    source_id = uuid4()
    template_id = uuid4()
    now = datetime.now(timezone.utc)

    monkeypatch.setattr(routes, "_upsert_run_import_health_summary", lambda *_args, **_kwargs: {"ok": True})

    push_conn(
        FakeConn(
            ones=[
                {"id": str(run_id), "firm_id": str(firm_id), "client_id": str(client_id), "status": "draft", "locked_at": None, "locked_by": None, "lock_reason": None},
                {"expected_headers": ["date", "amount", "account"], "expected_header_hash": "expected-hash"},
                {"id": str(source_id), "run_id": str(run_id), "file_kind": "bank", "uri": "s3://drift.csv", "checksum_sha256": "a" * 64, "created_at": now},
            ]
        )
    )
    payload = RegisterSourceFileRequest(
        file_kind="bank",
        filename="drift.csv",
        uri="s3://drift.csv",
        checksum_sha256="a" * 64,
        byte_size=10,
        mapping_template_id=template_id,
        observed_headers=["posted_on", "amount", "acct"],
    )
    result = routes.register_source_file(run_id, payload, user_id=user_id)
    assert str(result["id"]) == str(source_id)

    # Direct helper assertion for 409 drift blocker path.
    blocked_conn = FakeConn(alls=[[{"total": 1}]])
    with pytest.raises(HTTPException) as blocked:
        routes._ensure_no_import_drift_blocker(blocked_conn, str(run_id))
    assert blocked.value.status_code == 409


def test_enqueue_reconcile_bank_with_as_of_date(push_conn) -> None:
    user_id = "00000000-0000-0000-0000-00000000b003"
    run_id = uuid4()
    firm_id = uuid4()
    client_id = uuid4()
    now = datetime.now(timezone.utc)
    job_id = uuid4()

    push_conn(
        FakeConn(
            ones=[
                {"id": str(run_id), "firm_id": str(firm_id), "client_id": str(client_id), "status": "draft", "locked_at": None, "locked_by": None, "lock_reason": None},
                None,
                {"id": str(job_id), "run_id": str(run_id), "status": "queued", "job_type": "reconcile_bank", "queued_at": now},
            ],
            alls=[[{"total": 0}]],
        )
    )
    response = routes.enqueue_reconcile_bank(
        run_id,
        ReconcileBankRequest(payload={}, as_of_date=date(2025, 2, 25)),
        user_id=user_id,
    )
    assert str(response["id"]) == str(job_id)
    assert response["job_type"] == "reconcile_bank"


def _batch_row(batch_id: UUID, firm_id: UUID, *, status: str, now: datetime, requested_clients: int = 1, created_runs: int = 0, queued_jobs: int = 0, succeeded_runs: int = 0, failed_runs: int = 0) -> dict[str, Any]:
    return {
        "id": str(batch_id),
        "firm_id": str(firm_id),
        "period_start": date(2025, 1, 1),
        "period_end": date(2025, 1, 31),
        "status": status,
        "requested_clients": requested_clients,
        "created_runs": created_runs,
        "queued_jobs": queued_jobs,
        "succeeded_runs": succeeded_runs,
        "failed_runs": failed_runs,
        "options": {"as_of_date": "2025-02-25"},
        "created_at": now,
        "updated_at": now,
    }


def test_create_batch_runs_success_and_get_batch(push_conn) -> None:
    user_id = "00000000-0000-0000-0000-00000000b004"
    firm_id = uuid4()
    client_id = uuid4()
    batch_id = uuid4()
    run_id = uuid4()
    bank_job_id = uuid4()
    gl_job_id = uuid4()
    item_id = uuid4()
    now = datetime.now(timezone.utc)

    push_conn(
        FakeConn(
            ones=[
                _batch_row(batch_id, firm_id, status="running", now=now),
                {"id": str(client_id), "firm_id": str(firm_id)},
                None,
                {"id": str(run_id), "firm_id": str(firm_id), "client_id": str(client_id), "status": "draft", "locked_at": None, "locked_by": None, "lock_reason": None},
                None,
                {"id": str(bank_job_id), "run_id": str(run_id), "status": "queued", "job_type": "reconcile_bank", "queued_at": now},
                None,
                {"id": str(gl_job_id), "run_id": str(run_id), "status": "queued", "job_type": "reconcile_gl", "queued_at": now},
                {"id": str(item_id)},
                _batch_row(batch_id, firm_id, status="succeeded", now=now, created_runs=1, queued_jobs=2, succeeded_runs=1, failed_runs=0),
                _batch_row(batch_id, firm_id, status="succeeded", now=now, created_runs=1, queued_jobs=2, succeeded_runs=1, failed_runs=0),
            ],
            alls=[
                [{"total": 0}],
                [{"total": 0}],
                [{"total": 0}],
                [
                    {
                        "id": str(item_id),
                        "client_id": str(client_id),
                        "run_id": str(run_id),
                        "status": "queued",
                        "bank_job_id": str(bank_job_id),
                        "gl_job_id": str(gl_job_id),
                        "error": {},
                        "updated_at": now,
                        "bank_job_status": "succeeded",
                        "gl_job_status": "succeeded",
                    }
                ],
            ],
        )
    )
    created = routes.create_batch_runs(
        CreateBatchRunsRequest(
            firm_id=firm_id,
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            client_ids=[client_id],
            as_of_date=date(2025, 2, 25),
        ),
        user_id=user_id,
    )
    assert created["status"] == "succeeded"
    assert len(created["items"]) == 1
    assert created["items"][0]["status"] == "succeeded"

    # get_batch_runs success path (includes _refresh_batch_status).
    push_conn(
        FakeConn(
            ones=[
                _batch_row(batch_id, firm_id, status="running", now=now),
                _batch_row(batch_id, firm_id, status="running", now=now, created_runs=1, queued_jobs=2, succeeded_runs=0, failed_runs=0),
            ],
            alls=[
                [
                    {
                        "id": str(item_id),
                        "client_id": str(client_id),
                        "run_id": str(run_id),
                        "status": "queued",
                        "bank_job_id": str(bank_job_id),
                        "gl_job_id": str(gl_job_id),
                        "error": {},
                        "updated_at": now,
                        "bank_job_status": "running",
                        "gl_job_status": "queued",
                    }
                ]
            ],
        )
    )
    fetched = routes.get_batch_runs(batch_id, user_id=user_id)
    assert fetched["status"] == "running"
    assert fetched["items"][0]["status"] == "running"

    push_conn(FakeConn(ones=[None]))
    with pytest.raises(HTTPException) as not_found:
        routes.get_batch_runs(uuid4(), user_id=user_id)
    assert not_found.value.status_code == 404


def test_create_batch_runs_partial_failure_and_dashboard_filters(push_conn) -> None:
    user_id = "00000000-0000-0000-0000-00000000b005"
    firm_id = uuid4()
    client_ok = uuid4()
    client_fail = uuid4()
    batch_id = uuid4()
    run_id = uuid4()
    bank_job_id = uuid4()
    gl_job_id = uuid4()
    item_ok = uuid4()
    item_fail = uuid4()
    now = datetime.now(timezone.utc)

    push_conn(
        FakeConn(
            ones=[
                _batch_row(batch_id, firm_id, status="running", now=now, requested_clients=2),
                {"id": str(client_ok), "firm_id": str(firm_id)},
                {"id": str(run_id), "firm_id": str(firm_id), "client_id": str(client_ok), "status": "draft", "locked_at": None, "locked_by": None, "lock_reason": None},
                None,
                {"id": str(bank_job_id), "run_id": str(run_id), "status": "queued", "job_type": "reconcile_bank", "queued_at": now},
                None,
                {"id": str(gl_job_id), "run_id": str(run_id), "status": "queued", "job_type": "reconcile_gl", "queued_at": now},
                {"id": str(item_ok)},
                None,
                {"id": str(item_fail)},
                _batch_row(batch_id, firm_id, status="partially_failed", now=now, requested_clients=2, created_runs=0, queued_jobs=2, succeeded_runs=1, failed_runs=1),
                _batch_row(batch_id, firm_id, status="partially_failed", now=now, requested_clients=2, created_runs=0, queued_jobs=2, succeeded_runs=1, failed_runs=1),
            ],
            alls=[
                [{"total": 0}],
                [{"total": 0}],
                [{"total": 0}],
                [
                    {
                        "id": str(item_ok),
                        "client_id": str(client_ok),
                        "run_id": str(run_id),
                        "status": "queued",
                        "bank_job_id": str(bank_job_id),
                        "gl_job_id": str(gl_job_id),
                        "error": {},
                        "updated_at": now,
                        "bank_job_status": "succeeded",
                        "gl_job_status": "succeeded",
                    },
                    {
                        "id": str(item_fail),
                        "client_id": str(client_fail),
                        "run_id": None,
                        "status": "failed",
                        "bank_job_id": None,
                        "gl_job_id": None,
                        "error": {"message": "Client not found for firm"},
                        "updated_at": now,
                        "bank_job_status": "failed",
                        "gl_job_status": None,
                    },
                ],
            ],
        )
    )
    partial = routes.create_batch_runs(
        CreateBatchRunsRequest(
            firm_id=firm_id,
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            client_ids=[client_ok, client_fail],
            as_of_date=date(2025, 2, 25),
        ),
        user_id=user_id,
    )
    assert partial["status"] == "partially_failed"
    assert len(partial["items"]) == 2

    # Dashboard filters and deterministic ordering.
    today = date.today()
    dashboard_rows: list[dict[str, Any]] = [
        {
            "run_id": str(uuid4()),
            "client_id": str(client_ok),
            "client_name": "Alpha",
            "run_status": "completed",
            "payday_date": today,
            "must_close_by_date": today - timedelta(days=1),
            "sla_reminder_state": "overdue",
            "bank_status": "Not tied",
            "gl_status": "Tied",
            "open_blockers": 1,
            "open_review": 0,
            "import_health_band": "red",
            "latest_job_status": "failed",
        },
        {
            "run_id": str(uuid4()),
            "client_id": str(client_fail),
            "client_name": "Beta",
            "run_status": "completed",
            "payday_date": today,
            "must_close_by_date": today,
            "sla_reminder_state": "due_soon",
            "bank_status": "Tied",
            "gl_status": "Tied",
            "open_blockers": 0,
            "open_review": 1,
            "import_health_band": "amber",
            "latest_job_status": "running",
        },
        {
            "run_id": str(uuid4()),
            "client_id": str(uuid4()),
            "client_name": "Gamma",
            "run_status": "approved",
            "payday_date": today + timedelta(days=1),
            "must_close_by_date": today + timedelta(days=1),
            "sla_reminder_state": "on_track",
            "bank_status": "Tied",
            "gl_status": "Tied",
            "open_blockers": 0,
            "open_review": 0,
            "import_health_band": "green",
            "latest_job_status": "succeeded",
        },
        {
            "run_id": str(uuid4()),
            "client_id": str(uuid4()),
            "client_name": "Delta",
            "run_status": "approved",
            "payday_date": today + timedelta(days=30),
            "must_close_by_date": today + timedelta(days=30),
            "sla_reminder_state": "on_track",
            "bank_status": "Tied",
            "gl_status": "Tied",
            "open_blockers": 0,
            "open_review": 0,
            "import_health_band": "green",
            "latest_job_status": "succeeded",
        },
    ]

    push_conn(FakeConn(alls=[dashboard_rows]))
    dashboard_all = routes.bureau_dashboard(
        firm_id=firm_id,
        status_filter=None,
        limit=100,
        offset=0,
        user_id=user_id,
    )
    assert dashboard_all["total"] == 4
    assert dashboard_all["counts_by_due_bucket"]["overdue"] == 1

    push_conn(FakeConn(alls=[dashboard_rows]))
    dashboard_attention = routes.bureau_dashboard(
        firm_id=firm_id,
        status_filter=None,
        needs_attention=True,
        due_before=today,
        limit=100,
        offset=0,
        user_id=user_id,
    )
    assert dashboard_attention["total"] == 2

    target_client = UUID(str(dashboard_rows[3]["client_id"]))
    push_conn(FakeConn(alls=[dashboard_rows]))
    dashboard_filtered = routes.bureau_dashboard(
        firm_id=firm_id,
        status_filter="approved",
        client_id=target_client,
        limit=100,
        offset=0,
        user_id=user_id,
    )
    assert dashboard_filtered["total"] == 1
    assert dashboard_filtered["runs"][0]["client_name"] == "Delta"
