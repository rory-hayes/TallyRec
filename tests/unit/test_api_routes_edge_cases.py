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

    def download_bytes(self, _bucket: str, _object_path: str) -> bytes:
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

    monkeypatch.setattr(routes, "db_session", fake_db_session)
    monkeypatch.setattr(routes, "get_storage_client", lambda: FakeStorage())

    with TestClient(app) as client:
        yield client, conn_queue.append


def _run_row(run_id: str, firm_id: str, client_id: str, *, locked: bool = False) -> dict[str, Any]:
    return {
        "id": run_id,
        "firm_id": firm_id,
        "client_id": client_id,
        "status": "draft",
        "locked_at": datetime.now(timezone.utc) if locked else None,
        "locked_by": None,
        "lock_reason": None,
    }


def test_missing_entity_paths(client_and_push) -> None:
    client, push_conn = client_and_push
    user = "00000000-0000-0000-0000-00000000aa01"
    run_id = str(uuid4())

    push_conn(FakeConn(ones=[None]))
    assert client.post("/v1/clients", json={"firm_id": str(uuid4()), "name": "X"}, headers=_headers(user)).status_code == 404

    push_conn(FakeConn(ones=[None]))
    assert (
        client.post(
            "/v1/runs",
            json={"firm_id": str(uuid4()), "client_id": str(uuid4()), "period_start": "2025-01-01", "period_end": "2025-01-31"},
            headers=_headers(user),
        ).status_code
        == 404
    )

    push_conn(FakeConn(ones=[None]))
    assert (
        client.post(
            f"/v1/runs/{run_id}/source-files",
            json={"file_kind": "bank", "filename": "x.csv", "uri": "s3://x", "checksum_sha256": "a" * 64, "byte_size": 1},
            headers=_headers(user),
        ).status_code
        == 404
    )

    push_conn(FakeConn(ones=[None]))
    assert client.post(f"/v1/runs/{run_id}/jobs", json={"job_type": "noop", "payload": {}}, headers=_headers(user)).status_code == 404

    push_conn(FakeConn(ones=[None]))
    assert client.post(f"/v1/runs/{run_id}/reconcile/bank", json={"payload": {}}, headers=_headers(user)).status_code == 404

    push_conn(FakeConn(ones=[None]))
    assert client.post(f"/v1/runs/{run_id}/reconcile/gl", json={"payload": {}}, headers=_headers(user)).status_code == 404

    push_conn(FakeConn(ones=[None]))
    assert client.post(f"/v1/runs/{run_id}/export-pack", json={}, headers=_headers(user)).status_code == 404


def test_enqueue_and_match_group_paths(client_and_push) -> None:
    client, push_conn = client_and_push
    user = "00000000-0000-0000-0000-00000000aa02"
    now = datetime.now(timezone.utc)
    run_id = str(uuid4())
    firm_id = str(uuid4())
    client_id = str(uuid4())
    job_id = str(uuid4())

    push_conn(FakeConn(ones=[_run_row(run_id, firm_id, client_id), {"id": job_id, "run_id": run_id, "status": "queued", "job_type": "noop", "queued_at": now}]))
    resp = client.post(f"/v1/runs/{run_id}/jobs", json={"job_type": "noop", "payload": {}}, headers=_headers(user))
    assert resp.status_code == 201

    push_conn(
        FakeConn(
            ones=[_run_row(run_id, firm_id, client_id), {"id": str(uuid4()), "run_id": run_id, "status": "queued", "job_type": "reconcile_bank", "queued_at": now}]
        )
    )
    assert client.post(f"/v1/runs/{run_id}/reconcile/bank", json={"payload": {}}, headers=_headers(user)).status_code == 201

    push_conn(FakeConn(ones=[None]))
    assert client.get(f"/v1/runs/{run_id}/match-groups", headers=_headers(user)).status_code == 404

    push_conn(
        FakeConn(
            ones=[{"id": run_id}],
            alls=[[{"id": str(uuid4()), "group_kind": "one_to_many", "match_confidence": "deterministic", "expected_total": "100.00", "bank_total": "100.00", "delta": "0.00", "matched_on": "2025-01-31", "members_count": 2}]],
        )
    )
    groups = client.get(f"/v1/runs/{run_id}/match-groups", headers=_headers(user))
    assert groups.status_code == 200
    assert len(groups.json()) == 1


def test_summary_and_tieout_error_branches(client_and_push) -> None:
    client, push_conn = client_and_push
    user = "00000000-0000-0000-0000-00000000aa03"
    run_id = str(uuid4())
    firm_id = str(uuid4())
    client_id = str(uuid4())
    now = datetime.now(timezone.utc)

    push_conn(FakeConn(ones=[None]))
    assert client.get(f"/v1/runs/{run_id}/summary", headers=_headers(user)).status_code == 404

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
                    "bank_computed_at": now,
                    "gl_status": "Tied",
                    "gl_computed_at": now,
                    "approval_status": "pending",
                    "prepared_by": None,
                    "prepared_at": None,
                    "reviewer_id": None,
                    "reviewed_at": None,
                }
            ],
            alls=[[{"severity": "review", "total": 2}]],
        )
    )
    needs_review = client.get(f"/v1/runs/{run_id}/summary", headers=_headers(user))
    assert needs_review.status_code == 200
    assert needs_review.json()["overall_tie_status"] == "Needs review"

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
                    "bank_computed_at": now,
                    "gl_status": None,
                    "gl_computed_at": None,
                    "approval_status": "pending",
                    "prepared_by": None,
                    "prepared_at": None,
                    "reviewer_id": None,
                    "reviewed_at": None,
                }
            ],
            alls=[[]],
        )
    )
    not_tied = client.get(f"/v1/runs/{run_id}/summary", headers=_headers(user))
    assert not_tied.status_code == 200
    assert not_tied.json()["overall_tie_status"] == "Not tied"

    push_conn(FakeConn(ones=[None]))
    assert client.get(f"/v1/runs/{run_id}/bank-tieout", headers=_headers(user)).status_code == 404

    push_conn(FakeConn(ones=[None]))
    assert client.get(f"/v1/runs/{run_id}/gl-tieout", headers=_headers(user)).status_code == 404


def test_variance_resolution_error_paths(client_and_push) -> None:
    client, push_conn = client_and_push
    user = "00000000-0000-0000-0000-00000000aa04"
    run_id = str(uuid4())
    variance_id = str(uuid4())

    push_conn(FakeConn(ones=[None]))
    assert client.get(f"/v1/runs/{run_id}/variances?status=open", headers=_headers(user)).status_code == 404

    push_conn(FakeConn(ones=[{"id": run_id}], alls=[[]]))
    listed = client.get(f"/v1/runs/{run_id}/variances?status=open&code=BNK-001", headers=_headers(user))
    assert listed.status_code == 200
    assert listed.json() == []

    push_conn(FakeConn(ones=[None]))
    assert client.get(f"/v1/runs/{run_id}/variances/{variance_id}", headers=_headers(user)).status_code == 404

    invalid_action = client.post(
        f"/v1/variances/{variance_id}/resolve",
        json={"action": "nonsense", "note": "x"},
        headers=_headers(user),
    )
    assert invalid_action.status_code == 422

    missing_note = client.post(
        f"/v1/variances/{variance_id}/resolve",
        json={"action": "matched", "note": "   "},
        headers=_headers(user),
    )
    assert missing_note.status_code == 422

    push_conn(FakeConn(ones=[None]))
    not_found = client.post(
        f"/v1/variances/{variance_id}/resolve",
        json={"action": "matched", "note": "ok"},
        headers=_headers(user),
    )
    assert not_found.status_code == 404


def test_approve_ignored_error_paths(client_and_push) -> None:
    client, push_conn = client_and_push
    user = "00000000-0000-0000-0000-00000000aa05"
    run_id = str(uuid4())
    variance_id = str(uuid4())
    firm_id = str(uuid4())
    client_id = str(uuid4())

    push_conn(FakeConn(ones=[None]))
    assert client.post(f"/v1/variances/{variance_id}/approve-ignored", headers=_headers(user)).status_code == 404

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
                {"role": "viewer"},
            ]
        )
    )
    assert client.post(f"/v1/variances/{variance_id}/approve-ignored", headers=_headers(user)).status_code == 403

    push_conn(
        FakeConn(
            ones=[
                {
                    "id": variance_id,
                    "run_id": run_id,
                    "firm_id": firm_id,
                    "client_id": client_id,
                    "locked_at": None,
                    "resolution_action": "explained",
                    "ignored_needs_reviewer_approval": False,
                },
                {"role": "admin"},
            ]
        )
    )
    assert client.post(f"/v1/variances/{variance_id}/approve-ignored", headers=_headers(user)).status_code == 400


def test_policy_bank_accounts_and_gl_bucket_paths(client_and_push) -> None:
    client, push_conn = client_and_push
    user = "00000000-0000-0000-0000-00000000aa06"
    client_id = str(uuid4())
    firm_id = str(uuid4())

    push_conn(FakeConn(ones=[None]))
    assert client.patch(f"/v1/clients/{client_id}/recon-policy", json={"amount_tolerance": 0.02}, headers=_headers(user)).status_code == 404

    push_conn(FakeConn(ones=[{"client_id": client_id, "firm_id": firm_id, "amount_tolerance": "0.01"}]))
    assert client.patch(f"/v1/clients/{client_id}/recon-policy", json={"amount_tolerance": 0.02}, headers=_headers(user)).status_code == 200

    push_conn(FakeConn(ones=[None]))
    assert client.put(f"/v1/clients/{client_id}/bank-accounts", json={"accounts": []}, headers=_headers(user)).status_code == 404

    push_conn(
        FakeConn(
            ones=[{"id": client_id, "firm_id": firm_id}],
            alls=[[{"id": str(uuid4()), "account_ref": "A-1", "label": "Main"}]],
        )
    )
    bank_accounts = client.put(
        f"/v1/clients/{client_id}/bank-accounts",
        json={"accounts": [{"account_ref": "A-1", "label": "Main"}]},
        headers=_headers(user),
    )
    assert bank_accounts.status_code == 200
    assert bank_accounts.json()["accounts"][0]["account_ref"] == "A-1"

    push_conn(FakeConn(ones=[None]))
    assert (
        client.put(
            f"/v1/clients/{client_id}/gl-bucket-accounts",
            json={"net_pay_control": ["2200"]},
            headers=_headers(user),
        ).status_code
        == 404
    )

    push_conn(
        FakeConn(
            ones=[{"id": client_id, "firm_id": firm_id}],
            alls=[[{"bucket": "net_pay_control", "account_code": "2200"}]],
        )
    )
    gl_map = client.put(
        f"/v1/clients/{client_id}/gl-bucket-accounts",
        json={"net_pay_control": ["2200", "2200"], "taxes": [], "pension": [], "other": []},
        headers=_headers(user),
    )
    assert gl_map.status_code == 200
    assert gl_map.json()["buckets"][0]["bucket"] == "net_pay_control"


def test_ready_approve_unlock_errors(client_and_push) -> None:
    client, push_conn = client_and_push
    user = "00000000-0000-0000-0000-00000000aa07"
    reviewer = "00000000-0000-0000-0000-00000000aa08"
    run_id = str(uuid4())
    firm_id = str(uuid4())
    client_id = str(uuid4())

    push_conn(FakeConn(ones=[None]))
    assert client.post(f"/v1/runs/{run_id}/ready-for-review", json={"note": "x"}, headers=_headers(user)).status_code == 404

    push_conn(FakeConn(ones=[_run_row(run_id, firm_id, client_id), {"role": "viewer"}]))
    assert client.post(f"/v1/runs/{run_id}/ready-for-review", json={"note": "x"}, headers=_headers(user)).status_code == 403

    push_conn(FakeConn(ones=[None]))
    assert client.post(f"/v1/runs/{run_id}/approve", json={}, headers=_headers(reviewer)).status_code == 404

    push_conn(FakeConn(ones=[_run_row(run_id, firm_id, client_id), {"role": "viewer"}]))
    assert client.post(f"/v1/runs/{run_id}/approve", json={}, headers=_headers(reviewer)).status_code == 403

    push_conn(FakeConn(ones=[_run_row(run_id, firm_id, client_id), {"role": "admin"}, None]))
    assert client.post(f"/v1/runs/{run_id}/approve", json={}, headers=_headers(reviewer)).status_code == 400

    push_conn(
        FakeConn(
            ones=[
                _run_row(run_id, firm_id, client_id),
                {"role": "admin"},
                {"id": str(uuid4()), "prepared_by": user},
                {"blocker_open": 1, "ignored_pending": 0},
            ]
        )
    )
    assert client.post(f"/v1/runs/{run_id}/approve", json={}, headers=_headers(reviewer)).status_code == 400

    push_conn(
        FakeConn(
            ones=[
                _run_row(run_id, firm_id, client_id),
                {"role": "admin"},
                {"id": str(uuid4()), "prepared_by": user},
                {"blocker_open": 0, "ignored_pending": 1},
            ]
        )
    )
    assert client.post(f"/v1/runs/{run_id}/approve", json={}, headers=_headers(reviewer)).status_code == 400

    push_conn(FakeConn(ones=[None]))
    assert client.post(f"/v1/runs/{run_id}/unlock", json={"reason": "fix"}, headers=_headers(reviewer)).status_code == 404

    push_conn(FakeConn(ones=[_run_row(run_id, firm_id, client_id, locked=True), {"role": "viewer"}]))
    assert client.post(f"/v1/runs/{run_id}/unlock", json={"reason": "fix"}, headers=_headers(reviewer)).status_code == 403


def test_export_pack_list_download_errors(client_and_push) -> None:
    client, push_conn = client_and_push
    user = "00000000-0000-0000-0000-00000000aa09"
    run_id = str(uuid4())
    pack_id = str(uuid4())

    push_conn(FakeConn(ones=[None]))
    assert client.get(f"/v1/runs/{run_id}/export-packs", headers=_headers(user)).status_code == 404

    push_conn(FakeConn(ones=[None]))
    assert client.get(f"/v1/export-packs/{pack_id}/download", headers=_headers(user)).status_code == 404

    push_conn(FakeConn(ones=[{"id": pack_id, "storage_bucket": None, "storage_path": None}]))
    assert client.get(f"/v1/export-packs/{pack_id}/download", headers=_headers(user)).status_code == 400
