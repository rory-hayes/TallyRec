from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import app
import apps.api.app.api.routes as routes


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


class FakeStorage:
    def __init__(self, payload: bytes = b"zip") -> None:
        self.payload = payload
        self.downloaded: list[tuple[str, str]] = []

    def download_bytes(self, bucket: str, object_path: str) -> bytes:
        self.downloaded.append((bucket, object_path))
        return self.payload


def _headers(user_id: str) -> dict[str, str]:
    return {"X-User-Id": user_id}


@pytest.fixture()
def client_and_push(monkeypatch: pytest.MonkeyPatch):
    conn_queue: deque[FakeConn] = deque()

    @contextmanager
    def fake_db_session(_user_id: str):
        if not conn_queue:
            raise AssertionError("No fake connection queued for request")
        yield conn_queue.popleft()

    storage = FakeStorage()

    monkeypatch.setattr(routes, "db_session", fake_db_session)
    monkeypatch.setattr(routes, "get_storage_client", lambda: storage)

    with TestClient(app) as client:
        yield client, conn_queue.append, storage


def test_health_and_auth(client_and_push) -> None:
    client, _, _ = client_and_push
    assert client.get("/v1/health").status_code == 200
    assert client.post("/v1/firms", json={"name": "x", "slug": "x"}).status_code == 401


def test_core_creation_and_enqueues(client_and_push) -> None:
    client, push_conn, _ = client_and_push
    user = "00000000-0000-0000-0000-000000001111"

    now = datetime.now(timezone.utc)
    firm_id = str(uuid4())
    client_id = str(uuid4())
    run_id = str(uuid4())
    source_id = str(uuid4())
    job_id = str(uuid4())

    push_conn(FakeConn(ones=[{"id": firm_id, "name": "Firm", "slug": "firm", "created_at": now}]))
    firm = client.post("/v1/firms", json={"name": "Firm", "slug": "firm"}, headers=_headers(user))
    assert firm.status_code == 201

    push_conn(
        FakeConn(
            ones=[
                {"id": firm_id},
                {"id": client_id, "firm_id": firm_id, "name": "Client", "external_ref": "X", "created_at": now},
            ]
        )
    )
    create_client = client.post(
        "/v1/clients",
        json={"firm_id": firm_id, "name": "Client", "external_ref": "X"},
        headers=_headers(user),
    )
    assert create_client.status_code == 201

    push_conn(
        FakeConn(
            ones=[
                {"id": client_id},
                {
                    "id": run_id,
                    "firm_id": firm_id,
                    "client_id": client_id,
                    "status": "draft",
                    "period_start": "2025-01-01",
                    "period_end": "2025-01-31",
                    "created_at": now,
                },
            ]
        )
    )
    run = client.post(
        "/v1/runs",
        json={"firm_id": firm_id, "client_id": client_id, "period_start": "2025-01-01", "period_end": "2025-01-31"},
        headers=_headers(user),
    )
    assert run.status_code == 201

    push_conn(
        FakeConn(
            ones=[
                {
                    "id": run_id,
                    "firm_id": firm_id,
                    "client_id": client_id,
                    "status": "draft",
                    "locked_at": None,
                    "locked_by": None,
                    "lock_reason": None,
                },
                {
                    "id": source_id,
                    "run_id": run_id,
                    "file_kind": "bank",
                    "uri": "s3://f",
                    "checksum_sha256": "a" * 64,
                    "created_at": now,
                },
            ]
        )
    )
    source = client.post(
        f"/v1/runs/{run_id}/source-files",
        json={"file_kind": "bank", "filename": "f.csv", "uri": "s3://f", "checksum_sha256": "a" * 64, "byte_size": 1},
        headers=_headers(user),
    )
    assert source.status_code == 201

    push_conn(
        FakeConn(
            ones=[
                {
                    "id": run_id,
                    "firm_id": firm_id,
                    "client_id": client_id,
                    "status": "draft",
                    "locked_at": None,
                    "locked_by": None,
                    "lock_reason": None,
                },
                {"id": job_id, "run_id": run_id, "status": "queued", "job_type": "reconcile_gl", "queued_at": now},
            ]
        )
    )
    recon_gl = client.post(f"/v1/runs/{run_id}/reconcile/gl", json={"payload": {}}, headers=_headers(user))
    assert recon_gl.status_code == 201

    push_conn(
        FakeConn(
            ones=[
                {
                    "id": run_id,
                    "firm_id": firm_id,
                    "client_id": client_id,
                    "status": "draft",
                    "locked_at": None,
                    "locked_by": None,
                    "lock_reason": None,
                },
                {"id": str(uuid4()), "run_id": run_id, "status": "queued", "job_type": "export_pack", "queued_at": now},
            ]
        )
    )
    export_job = client.post(f"/v1/runs/{run_id}/export-pack", json={}, headers=_headers(user))
    assert export_job.status_code == 201


def test_locking_and_rbac_workflow(client_and_push) -> None:
    client, push_conn, _ = client_and_push
    preparer = "00000000-0000-0000-0000-000000001112"
    reviewer = "00000000-0000-0000-0000-000000001113"
    run_id = str(uuid4())
    firm_id = str(uuid4())
    client_id = str(uuid4())

    # Locked run blocks source file edit.
    push_conn(
        FakeConn(
            ones=[
                {
                    "id": run_id,
                    "firm_id": firm_id,
                    "client_id": client_id,
                    "status": "approved",
                    "locked_at": datetime.now(timezone.utc),
                    "locked_by": reviewer,
                    "lock_reason": "reviewer_approval",
                }
            ]
        )
    )
    blocked = client.post(
        f"/v1/runs/{run_id}/source-files",
        json={"file_kind": "bank", "filename": "f.csv", "uri": "s3://f", "checksum_sha256": "a" * 64, "byte_size": 1},
        headers=_headers(preparer),
    )
    assert blocked.status_code == 403

    # Ready for review success.
    approval_id = str(uuid4())
    push_conn(
        FakeConn(
            ones=[
                {
                    "id": run_id,
                    "firm_id": firm_id,
                    "client_id": client_id,
                    "status": "completed",
                    "locked_at": None,
                    "locked_by": None,
                    "lock_reason": None,
                },
                {"role": "analyst"},
                {
                    "id": approval_id,
                    "run_id": run_id,
                    "status": "pending",
                    "prepared_by": preparer,
                    "prepared_at": datetime.now(timezone.utc),
                    "reviewer_id": None,
                    "reviewed_at": None,
                },
            ]
        )
    )
    ready = client.post(f"/v1/runs/{run_id}/ready-for-review", json={"note": "ready"}, headers=_headers(preparer))
    assert ready.status_code == 200

    # Preparer cannot self approve.
    push_conn(
        FakeConn(
            ones=[
                {
                    "id": run_id,
                    "firm_id": firm_id,
                    "client_id": client_id,
                    "status": "ready_for_review",
                    "locked_at": None,
                    "locked_by": None,
                    "lock_reason": None,
                },
                {"role": "admin"},
                {"id": approval_id, "prepared_by": preparer},
            ]
        )
    )
    self_approve = client.post(f"/v1/runs/{run_id}/approve", json={}, headers=_headers(preparer))
    assert self_approve.status_code == 403

    # Reviewer approve success.
    push_conn(
        FakeConn(
            ones=[
                {
                    "id": run_id,
                    "firm_id": firm_id,
                    "client_id": client_id,
                    "status": "ready_for_review",
                    "locked_at": None,
                    "locked_by": None,
                    "lock_reason": None,
                },
                {"role": "admin"},
                {"id": approval_id, "prepared_by": preparer},
                {"blocker_open": 0, "ignored_pending": 0},
                {
                    "id": approval_id,
                    "run_id": run_id,
                    "status": "approved",
                    "prepared_by": preparer,
                    "reviewer_id": reviewer,
                    "reviewed_at": datetime.now(timezone.utc),
                },
                {
                    "id": run_id,
                    "status": "approved",
                    "locked_at": datetime.now(timezone.utc),
                    "locked_by": reviewer,
                    "lock_reason": "reviewer_approval",
                },
            ]
        )
    )
    approved = client.post(f"/v1/runs/{run_id}/approve", json={"note": "ok"}, headers=_headers(reviewer))
    assert approved.status_code == 200

    # Unlock success.
    push_conn(
        FakeConn(
            ones=[
                {
                    "id": run_id,
                    "firm_id": firm_id,
                    "client_id": client_id,
                    "status": "approved",
                    "locked_at": datetime.now(timezone.utc),
                    "locked_by": reviewer,
                    "lock_reason": "reviewer_approval",
                },
                {"role": "owner"},
                {"id": run_id, "status": "completed", "locked_at": None, "lock_reason": "manual_fix"},
            ]
        )
    )
    unlock = client.post(f"/v1/runs/{run_id}/unlock", json={"reason": "manual_fix"}, headers=_headers(reviewer))
    assert unlock.status_code == 200


def test_variance_center_and_resolution(client_and_push) -> None:
    client, push_conn, _ = client_and_push
    user = "00000000-0000-0000-0000-000000001114"
    reviewer = "00000000-0000-0000-0000-000000001115"
    run_id = str(uuid4())
    firm_id = str(uuid4())
    client_id = str(uuid4())
    variance_id = str(uuid4())

    push_conn(
        FakeConn(
            ones=[
                {
                    "id": run_id,
                    "run_status": "completed",
                    "firm_id": firm_id,
                    "client_id": client_id,
                    "locked_at": None,
                    "locked_by": None,
                    "expected_net_pay": "100.00",
                    "matched_bank_total": "100.00",
                    "delta": "0.00",
                    "bank_status": "Tied",
                    "bank_computed_at": datetime.now(timezone.utc),
                    "gl_status": "Not tied",
                    "gl_computed_at": datetime.now(timezone.utc),
                    "approval_status": "pending",
                    "prepared_by": user,
                    "prepared_at": datetime.now(timezone.utc),
                    "reviewer_id": None,
                    "reviewed_at": None,
                }
            ],
            alls=[[{"severity": "blocker", "total": 1}]],
        )
    )
    summary = client.get(f"/v1/runs/{run_id}/summary", headers=_headers(user))
    assert summary.status_code == 200
    assert summary.json()["overall_tie_status"] == "Not tied"

    push_conn(
        FakeConn(
            ones=[{"id": run_id}],
            alls=[
                [
                    {
                        "id": variance_id,
                        "code": "GL-001",
                        "category": "gl",
                        "severity": "blocker",
                        "status": "open",
                        "message": "missing",
                        "amount": "100.00",
                        "event_date": None,
                        "account_ref": None,
                        "details": {},
                        "note": None,
                        "changed_by": None,
                        "changed_at": None,
                        "resolution_action": None,
                        "ignored_needs_reviewer_approval": False,
                        "ignored_approved_by": None,
                        "ignored_approved_at": None,
                    }
                ]
            ],
        )
    )
    listing = client.get(f"/v1/runs/{run_id}/variances?status=open&category=gl", headers=_headers(user))
    assert listing.status_code == 200

    push_conn(
        FakeConn(
            ones=[
                {
                    "id": variance_id,
                    "run_id": run_id,
                    "firm_id": firm_id,
                    "client_id": client_id,
                    "status": "open",
                    "severity": "blocker",
                    "resolution_action": None,
                    "ignored_needs_reviewer_approval": False,
                }
            ],
            alls=[[{"id": str(uuid4()), "action": "created", "note": None, "actor_user_id": user, "created_at": datetime.now(timezone.utc)}]],
        )
    )
    detail = client.get(f"/v1/runs/{run_id}/variances/{variance_id}", headers=_headers(user))
    assert detail.status_code == 200

    # Resolve ignored success.
    push_conn(
        FakeConn(
            ones=[
                {"id": variance_id, "run_id": run_id, "firm_id": firm_id, "client_id": client_id, "locked_at": None},
                {
                    "id": variance_id,
                    "run_id": run_id,
                    "firm_id": firm_id,
                    "client_id": client_id,
                    "status": "ignored",
                    "resolution_action": "ignored",
                    "ignored_needs_reviewer_approval": True,
                },
            ]
        )
    )
    resolved = client.post(
        f"/v1/variances/{variance_id}/resolve",
        json={"action": "ignored", "note": "investigated"},
        headers=_headers(user),
    )
    assert resolved.status_code == 200

    # Reviewer approves ignored.
    push_conn(
        FakeConn(
            ones=[
                {
                    "id": variance_id,
                    "run_id": run_id,
                    "firm_id": firm_id,
                    "client_id": client_id,
                    "locked_at": None,
                    "resolution_action": "ignored",
                    "ignored_needs_reviewer_approval": True,
                },
                {"role": "admin"},
                {
                    "id": variance_id,
                    "run_id": run_id,
                    "firm_id": firm_id,
                    "client_id": client_id,
                    "status": "ignored",
                    "ignored_needs_reviewer_approval": False,
                },
            ]
        )
    )
    approved = client.post(f"/v1/variances/{variance_id}/approve-ignored", headers=_headers(reviewer))
    assert approved.status_code == 200


def test_gl_tieout_export_and_download(client_and_push) -> None:
    client, push_conn, storage = client_and_push
    user = "00000000-0000-0000-0000-000000001116"
    run_id = str(uuid4())
    pack_id = str(uuid4())

    push_conn(
        FakeConn(
            ones=[
                {
                    "run_id": run_id,
                    "payroll_totals": {"net_pay_control": "100.00"},
                    "gl_totals": {"net_pay_control": "100.00"},
                    "deltas": {"net_pay_control": "0.00"},
                    "is_balanced": True,
                    "status": "Tied",
                    "rules_used": {"checks": []},
                    "computed_at": datetime.now(timezone.utc),
                }
            ]
        )
    )
    gl = client.get(f"/v1/runs/{run_id}/gl-tieout", headers=_headers(user))
    assert gl.status_code == 200

    push_conn(FakeConn(ones=[{"id": run_id}], alls=[[{"id": pack_id, "status": "generated", "pack_hash": "h", "storage_bucket": "audit-packs", "storage_path": "runs/a.zip", "generated_at": None, "error": None, "created_at": None, "updated_at": None}]]))
    packs = client.get(f"/v1/runs/{run_id}/export-packs", headers=_headers(user))
    assert packs.status_code == 200
    assert len(packs.json()) == 1

    push_conn(FakeConn(ones=[{"id": pack_id, "storage_bucket": "audit-packs", "storage_path": "runs/a.zip"}]))
    download = client.get(f"/v1/export-packs/{pack_id}/download", headers=_headers(user))
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/zip")
    assert storage.downloaded == [("audit-packs", "runs/a.zip")]
