from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg import Connection

from apps.worker import main as worker_main

pytestmark = pytest.mark.usefixtures("clean_db")


def _headers(user_id: str) -> dict[str, str]:
    return {"X-User-Id": user_id}


def _create_firm(api_client: TestClient, user_id: str) -> str:
    firm = api_client.post("/v1/firms", json={"name": "Batch Firm", "slug": f"batch-{uuid4().hex[:8]}"}, headers=_headers(user_id))
    assert firm.status_code == 201, firm.text
    return firm.json()["id"]


def _create_clients(api_client: TestClient, user_id: str, firm_id: str, total: int) -> list[str]:
    client_ids: list[str] = []
    for idx in range(total):
        response = api_client.post(
            "/v1/clients",
            json={"firm_id": firm_id, "name": f"Client {idx:02d}", "external_ref": f"C-{idx:02d}"},
            headers=_headers(user_id),
        )
        assert response.status_code == 201, response.text
        client_ids.append(response.json()["id"])
    return client_ids


def test_batch_processing_isolates_failures(api_client: TestClient, db_conn: Connection, db_url: str) -> None:
    user = "00000000-0000-0000-0000-00000000c001"
    firm_id = _create_firm(api_client, user)
    client_ids = _create_clients(api_client, user, firm_id, total=10)

    created = api_client.post(
        "/v1/batches/runs",
        json={
            "firm_id": firm_id,
            "period_start": "2025-01-01",
            "period_end": "2025-01-31",
            "client_ids": client_ids,
            "as_of_date": "2025-02-23",
        },
        headers=_headers(user),
    )
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]
    items = created.json()["items"]
    assert len(items) == 10

    locked_run_id = next(item["run_id"] for item in items if item["run_id"] is not None)
    with db_conn.cursor() as cur:
        cur.execute(
            """
            update public.runs
            set locked_at = now(), lock_reason = 'test_lock'
            where id = %s
            """,
            (locked_run_id,),
        )
    db_conn.commit()

    worker_main.DATABASE_URL = db_url
    for _ in range(100):
        if not worker_main.run_once():
            break

    batch = api_client.get(f"/v1/batches/{batch_id}", headers=_headers(user))
    assert batch.status_code == 200, batch.text
    payload = batch.json()
    assert payload["status"] == "partially_failed"
    assert payload["failed_runs"] >= 1
    assert payload["succeeded_runs"] >= 9

    with db_conn.cursor() as cur:
        cur.execute(
            """
            select
              count(*) filter (where status = 'failed')::int as failed_runs,
              count(*) filter (where status = 'completed')::int as completed_runs
            from public.runs
            where firm_id = %s
            """,
            (firm_id,),
        )
        counts = cur.fetchone()
        assert counts["failed_runs"] >= 1
        assert counts["completed_runs"] >= 9

        cur.execute(
            """
            select count(*)::int as total
            from public.audit_events
            where event_type = 'batch.completed' and firm_id = %s
            """,
            (firm_id,),
        )
        assert cur.fetchone()["total"] >= 1
