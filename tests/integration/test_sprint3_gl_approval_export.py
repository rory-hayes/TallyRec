from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg import Connection

from apps.worker import main as worker_main

pytestmark = pytest.mark.usefixtures("clean_db")


def _headers(user_id: str) -> dict[str, str]:
    return {"X-User-Id": user_id}


def _create_firm_client_run(api_client: TestClient, user_id: str) -> tuple[str, str, str]:
    firm = api_client.post("/v1/firms", json={"name": "Firm S3", "slug": f"firm-s3-{uuid4().hex[:8]}"}, headers=_headers(user_id))
    assert firm.status_code == 201, firm.text
    firm_id = firm.json()["id"]

    client = api_client.post(
        "/v1/clients",
        json={"firm_id": firm_id, "name": "Client S3", "external_ref": "S3-C1"},
        headers=_headers(user_id),
    )
    assert client.status_code == 201, client.text
    client_id = client.json()["id"]

    run = api_client.post(
        "/v1/runs",
        json={"firm_id": firm_id, "client_id": client_id, "period_start": "2025-01-01", "period_end": "2025-01-31"},
        headers=_headers(user_id),
    )
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]
    return firm_id, client_id, run_id


def test_gl_unbalanced_journal_creates_blocker(api_client: TestClient, db_conn: Connection, db_url: str) -> None:
    user = "00000000-0000-0000-0000-00000000b001"
    firm_id, client_id, run_id = _create_firm_client_run(api_client, user)

    gl_map = api_client.put(
        f"/v1/clients/{client_id}/gl-bucket-accounts",
        json={"net_pay_control": ["2200"], "taxes": [], "pension": [], "other": []},
        headers=_headers(user),
    )
    assert gl_map.status_code == 200, gl_map.text

    with db_conn.cursor() as cur:
        exp_id = str(uuid4())
        gl1 = str(uuid4())
        gl2 = str(uuid4())
        cur.execute(
            """
            insert into public.payroll_expected(
              id, run_id, firm_id, client_id, payment_date, net_amount,
              tax_amount, pension_amount, other_amount, deterministic_hash
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (exp_id, run_id, firm_id, client_id, date(2025, 1, 31), Decimal("100.00"), Decimal("0.00"), Decimal("0.00"), Decimal("0.00"), f"exp-{exp_id}"),
        )
        cur.execute(
            """
            insert into public.gl_journal_lines(
              id, run_id, firm_id, client_id, entry_date, account_code,
              debit_amount, credit_amount, deterministic_hash
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (gl1, run_id, firm_id, client_id, date(2025, 1, 31), "2200", Decimal("0.00"), Decimal("100.00"), f"gl-{gl1}"),
        )
        cur.execute(
            """
            insert into public.gl_journal_lines(
              id, run_id, firm_id, client_id, entry_date, account_code,
              debit_amount, credit_amount, deterministic_hash
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (gl2, run_id, firm_id, client_id, date(2025, 1, 31), "1000", Decimal("90.00"), Decimal("0.00"), f"gl-{gl2}"),
        )
    db_conn.commit()

    enqueued = api_client.post(
        f"/v1/runs/{run_id}/reconcile/gl",
        json={"idempotency_key": "gl-1", "payload": {}},
        headers=_headers(user),
    )
    assert enqueued.status_code == 201, enqueued.text

    worker_main.DATABASE_URL = db_url
    assert worker_main.run_once() is True

    gl_tieout = api_client.get(f"/v1/runs/{run_id}/gl-tieout", headers=_headers(user))
    assert gl_tieout.status_code == 200
    assert gl_tieout.json()["status"] == "Not tied"
    assert gl_tieout.json()["is_balanced"] is False

    variances = api_client.get(f"/v1/runs/{run_id}/variances?category=gl&status=open", headers=_headers(user))
    assert variances.status_code == 200
    codes = {item["code"] for item in variances.json()}
    assert "GL-002" in codes


def test_approval_rbac_and_locked_run_immutability(api_client: TestClient, db_conn: Connection) -> None:
    preparer = "00000000-0000-0000-0000-00000000b010"
    reviewer = "00000000-0000-0000-0000-00000000b011"
    firm_id, client_id, run_id = _create_firm_client_run(api_client, preparer)

    with db_conn.cursor() as cur:
        cur.execute("insert into auth.users(id, email) values (%s, %s)", (reviewer, "reviewer@example.com"))
        cur.execute(
            "insert into public.firm_memberships(firm_id, user_id, role) values (%s, %s, 'admin')",
            (firm_id, reviewer),
        )
    db_conn.commit()

    ready = api_client.post(f"/v1/runs/{run_id}/ready-for-review", json={"note": "ready"}, headers=_headers(preparer))
    assert ready.status_code == 200, ready.text

    self_approve = api_client.post(f"/v1/runs/{run_id}/approve", json={"note": "self"}, headers=_headers(preparer))
    assert self_approve.status_code == 403

    approved = api_client.post(f"/v1/runs/{run_id}/approve", json={"note": "ok"}, headers=_headers(reviewer))
    assert approved.status_code == 200, approved.text
    assert approved.json()["run"]["status"] == "approved"

    blocked_edit = api_client.post(
        f"/v1/runs/{run_id}/source-files",
        json={
            "file_kind": "bank",
            "filename": "blocked.csv",
            "uri": "s3://bucket/blocked.csv",
            "checksum_sha256": "a" * 64,
            "byte_size": 1,
        },
        headers=_headers(preparer),
    )
    assert blocked_edit.status_code == 403

    with db_conn.cursor() as cur:
        cur.execute("select count(*) as total from public.audit_events where run_id = %s and event_type = 'run.edit_blocked'", (run_id,))
        assert cur.fetchone()["total"] >= 1


def test_export_pack_reproducible_and_downloadable(
    api_client: TestClient,
    db_conn: Connection,
    db_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path))
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    user = "00000000-0000-0000-0000-00000000b020"
    firm_id, client_id, run_id = _create_firm_client_run(api_client, user)

    with db_conn.cursor() as cur:
        cur.execute(
            """
            insert into public.run_bank_tieout_summaries(
              run_id, firm_id, client_id, expected_net_pay, matched_bank_total, delta, status, policy_snapshot
            ) values (%s, %s, %s, %s, %s, %s, 'Tied', '{}'::jsonb)
            """,
            (run_id, firm_id, client_id, Decimal("100.00"), Decimal("100.00"), Decimal("0.00")),
        )
        cur.execute(
            """
            insert into public.run_gl_tieout_summaries(
              run_id, firm_id, client_id, payroll_totals, gl_totals, deltas, is_balanced, status, rules_used
            ) values (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, true, 'Tied', %s::jsonb)
            """,
            (
                run_id,
                firm_id,
                client_id,
                '{"net_pay_control":"100.00","taxes":"0.00","pension":"0.00","other":"0.00"}',
                '{"net_pay_control":"100.00","taxes":"0.00","pension":"0.00","other":"0.00"}',
                '{"net_pay_control":"0.00","taxes":"0.00","pension":"0.00","other":"0.00"}',
                '{"checks":["journal_presence","journal_balanced","bucket_totals_compare"]}',
            ),
        )
        source_id = str(uuid4())
        cur.execute(
            """
            insert into public.source_files(
              id, run_id, firm_id, client_id, file_kind, filename, uri, checksum_sha256, byte_size, registered_by
            ) values (%s, %s, %s, %s, 'bank', %s, %s, %s, %s, %s)
            """,
            (source_id, run_id, firm_id, client_id, "bank.csv", "s3://bucket/bank.csv", "a" * 64, 123, user),
        )
        cur.execute(
            """
            insert into public.approvals(
              run_id, firm_id, client_id, status, prepared_by, prepared_at, reviewer_id, reviewed_at, notes
            ) values (%s, %s, %s, 'approved', %s, now(), %s, now(), 'signed')
            """,
            (run_id, firm_id, client_id, user, user),
        )
    db_conn.commit()

    first = api_client.post(f"/v1/runs/{run_id}/export-pack", json={"idempotency_key": "exp-1"}, headers=_headers(user))
    assert first.status_code == 201, first.text
    worker_main.DATABASE_URL = db_url
    assert worker_main.run_once() is True

    second = api_client.post(f"/v1/runs/{run_id}/export-pack", json={"idempotency_key": "exp-2"}, headers=_headers(user))
    assert second.status_code == 201, second.text
    assert worker_main.run_once() is True

    with db_conn.cursor() as cur:
        cur.execute("select id, pack_hash, status, storage_bucket, storage_path from public.export_packs where run_id = %s", (run_id,))
        rows = cur.fetchall()
        assert len(rows) == 1
        export_pack = rows[0]
        assert export_pack["status"] == "generated"
        assert export_pack["pack_hash"]
        assert export_pack["storage_bucket"] == "audit-packs"
        assert export_pack["storage_path"].endswith(f"{export_pack['pack_hash']}.zip")

    packs = api_client.get(f"/v1/runs/{run_id}/export-packs", headers=_headers(user))
    assert packs.status_code == 200
    assert len(packs.json()) == 1
    download = api_client.get(f"/v1/export-packs/{export_pack['id']}/download", headers=_headers(user))
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/zip")
    assert len(download.content) > 0
