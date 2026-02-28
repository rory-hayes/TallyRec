from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg import Connection

from apps.worker import main as worker_main

pytestmark = pytest.mark.usefixtures("clean_db")


def _headers(user_id: str) -> dict[str, str]:
    return {"X-User-Id": user_id}


def _create_firm_client_run(api_client: TestClient, user_id: str) -> tuple[str, str, str]:
    firm = api_client.post("/v1/firms", json={"name": "Firm A", "slug": f"firm-{uuid4().hex[:8]}"}, headers=_headers(user_id))
    assert firm.status_code == 201, firm.text
    firm_id = firm.json()["id"]

    client = api_client.post(
        "/v1/clients",
        json={"firm_id": firm_id, "name": "Client A", "external_ref": "C-1"},
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


def test_happy_path_noop_job(api_client: TestClient, db_conn: Connection, db_url: str) -> None:
    user = "00000000-0000-0000-0000-000000000111"
    _, _, run_id = _create_firm_client_run(api_client, user)

    source_file = api_client.post(
        f"/v1/runs/{run_id}/source-files",
        json={
            "file_kind": "bank",
            "filename": "bank.csv",
            "uri": "s3://bucket/bank.csv",
            "checksum_sha256": "a" * 64,
            "byte_size": 123,
        },
        headers=_headers(user),
    )
    assert source_file.status_code == 201, source_file.text

    enqueued = api_client.post(
        f"/v1/runs/{run_id}/jobs",
        json={"job_type": "noop", "payload": {}},
        headers=_headers(user),
    )
    assert enqueued.status_code == 201, enqueued.text

    worker_main.DATABASE_URL = db_url
    assert worker_main.run_once() is True

    with db_conn.cursor() as cur:
        cur.execute("select status from public.jobs where id = %s", (enqueued.json()["id"],))
        job = cur.fetchone()
        assert job["status"] == "succeeded"

        cur.execute("select status from public.runs where id = %s", (run_id,))
        run = cur.fetchone()
        assert run["status"] == "completed"


def test_reconcile_bank_persists_summary_and_outputs(api_client: TestClient, db_conn: Connection, db_url: str) -> None:
    user = "00000000-0000-0000-0000-000000000222"
    firm_id, client_id, run_id = _create_firm_client_run(api_client, user)

    accounts = api_client.put(
        f"/v1/clients/{client_id}/bank-accounts",
        json={"accounts": [{"account_ref": "ACCT-1", "label": "Payroll"}]},
        headers=_headers(user),
    )
    assert accounts.status_code == 200, accounts.text

    with db_conn.cursor() as cur:
        exp_id = str(uuid4())
        bnk_id = str(uuid4())
        cur.execute(
            """
            insert into public.payroll_expected(
              id, run_id, firm_id, client_id, payment_date, net_amount, deterministic_hash
            ) values (%s, %s, %s, %s, %s, %s, %s)
            """,
            (exp_id, run_id, firm_id, client_id, date(2025, 1, 31), Decimal("10000.00"), f"exp-{exp_id}"),
        )
        cur.execute(
            """
            insert into public.bank_transactions(
              id, run_id, firm_id, client_id, posted_date, amount_signed, account_ref, deterministic_hash
            ) values (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (bnk_id, run_id, firm_id, client_id, date(2025, 1, 31), Decimal("-10000.00"), "ACCT-1", f"bnk-{bnk_id}"),
        )
    db_conn.commit()

    enqueued = api_client.post(
        f"/v1/runs/{run_id}/reconcile/bank",
        json={"idempotency_key": "recon-1", "payload": {}},
        headers=_headers(user),
    )
    assert enqueued.status_code == 201, enqueued.text

    worker_main.DATABASE_URL = db_url
    assert worker_main.run_once() is True

    summary = api_client.get(f"/v1/runs/{run_id}/summary", headers=_headers(user))
    assert summary.status_code == 200
    assert summary.json()["tieout"]["status"] == "Tied"

    variances = api_client.get(f"/v1/runs/{run_id}/variances?category=bank&status=open", headers=_headers(user))
    assert variances.status_code == 200
    assert variances.json() == []


def test_tenant_boundary_and_missing_ids(api_client: TestClient) -> None:
    user_a = "00000000-0000-0000-0000-000000000333"
    user_b = "00000000-0000-0000-0000-000000000444"

    _, _, run_id = _create_firm_client_run(api_client, user_a)

    cross = api_client.get(f"/v1/runs/{run_id}/summary", headers=_headers(user_b))
    assert cross.status_code == 404

    missing_firm_client = api_client.post(
        "/v1/clients",
        json={"firm_id": str(uuid4()), "name": "Nope"},
        headers=_headers(user_a),
    )
    assert missing_firm_client.status_code == 404

    missing_run_client = api_client.post(
        "/v1/runs",
        json={
            "firm_id": str(uuid4()),
            "client_id": str(uuid4()),
            "period_start": "2025-01-01",
            "period_end": "2025-01-31",
        },
        headers=_headers(user_a),
    )
    assert missing_run_client.status_code == 404
