from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg import Connection

pytestmark = pytest.mark.usefixtures("clean_db")


def _headers(user_id: str) -> dict[str, str]:
    return {"X-User-Id": user_id}


def _create_client(api_client: TestClient, user_id: str, firm_id: str, suffix: str) -> str:
    client = api_client.post(
        "/v1/clients",
        json={"firm_id": firm_id, "name": f"Dash Client {suffix}", "external_ref": f"D-{suffix}"},
        headers=_headers(user_id),
    )
    assert client.status_code == 201, client.text
    return client.json()["id"]


def _create_run(api_client: TestClient, user_id: str, firm_id: str, client_id: str) -> str:
    run = api_client.post(
        "/v1/runs",
        json={"firm_id": firm_id, "client_id": client_id, "period_start": "2025-01-01", "period_end": "2025-01-31"},
        headers=_headers(user_id),
    )
    assert run.status_code == 201, run.text
    return run.json()["id"]


def test_dashboard_counts_and_filters(api_client: TestClient, db_conn: Connection) -> None:
    user = "00000000-0000-0000-0000-00000000d001"
    firm = api_client.post("/v1/firms", json={"name": "Dash Firm", "slug": f"dash-{uuid4().hex[:6]}"}, headers=_headers(user))
    assert firm.status_code == 201, firm.text
    firm_id = firm.json()["id"]
    client_1 = _create_client(api_client, user, firm_id, "a")
    client_2 = _create_client(api_client, user, firm_id, "b")
    client_3 = _create_client(api_client, user, firm_id, "c")

    run_1 = _create_run(api_client, user, firm_id, client_1)
    run_2 = _create_run(api_client, user, firm_id, client_2)
    run_3 = _create_run(api_client, user, firm_id, client_3)

    yesterday = date.today() - timedelta(days=1)
    today = date.today()
    future = date.today() + timedelta(days=5)

    with db_conn.cursor() as cur:
        cur.execute(
            """
            update public.runs
            set status = 'failed', must_close_by_date = %s, payday_date = %s, sla_reminder_state = 'overdue'
            where id = %s
            """,
            (yesterday, yesterday, run_1),
        )
        cur.execute(
            """
            update public.runs
            set status = 'completed', must_close_by_date = %s, payday_date = %s, sla_reminder_state = 'due_soon'
            where id = %s
            """,
            (today, today, run_2),
        )
        cur.execute(
            """
            update public.runs
            set status = 'approved', must_close_by_date = %s, payday_date = %s, sla_reminder_state = 'on_track'
            where id = %s
            """,
            (future, future, run_3),
        )
        cur.execute(
            """
            insert into public.run_import_health_summaries(run_id, firm_id, client_id, health_score, health_band, factors)
            values
              (%s, %s, %s, 45, 'red', '{}'::jsonb),
              (%s, %s, %s, 70, 'amber', '{}'::jsonb),
              (%s, %s, %s, 95, 'green', '{}'::jsonb)
            """,
            (run_1, firm_id, client_1, run_2, firm_id, client_2, run_3, firm_id, client_3),
        )
        cur.execute(
            """
            insert into public.variances(run_id, firm_id, client_id, code, category, severity, status, message)
            values (%s, %s, %s, 'BNK-001', 'bank', 'blocker', 'open', 'missing')
            """,
            (run_1, firm_id, client_1),
        )
    db_conn.commit()

    dashboard = api_client.get(f"/v1/dashboard?firm_id={firm_id}", headers=_headers(user))
    assert dashboard.status_code == 200, dashboard.text
    payload = dashboard.json()
    assert payload["total"] == 3
    assert payload["counts_by_status"]["failed"] == 1
    assert payload["counts_by_status"]["completed"] == 1
    assert payload["counts_by_status"]["approved"] == 1
    assert payload["counts_by_due_bucket"]["overdue"] == 1
    assert payload["counts_by_due_bucket"]["due_today"] == 1
    assert payload["counts_by_due_bucket"]["later"] == 1

    attention = api_client.get(f"/v1/dashboard?firm_id={firm_id}&needs_attention=true", headers=_headers(user))
    assert attention.status_code == 200
    assert attention.json()["total"] == 2


def test_import_drift_blocks_reconcile_enqueue(api_client: TestClient, db_conn: Connection) -> None:
    user = "00000000-0000-0000-0000-00000000d010"
    firm = api_client.post("/v1/firms", json={"name": "Drift Firm", "slug": f"drift-{uuid4().hex[:6]}"}, headers=_headers(user))
    assert firm.status_code == 201, firm.text
    firm_id = firm.json()["id"]
    client_id = _create_client(api_client, user, firm_id, "drift")
    run_id = _create_run(api_client, user, firm_id, client_id)

    mapping_template_id = str(uuid4())
    with db_conn.cursor() as cur:
        cur.execute(
            """
            insert into public.mapping_templates(id, firm_id, client_id, name, file_kind, mapping, expected_headers, expected_header_hash)
            values (%s, %s, %s, %s, 'bank', '{}'::jsonb, %s::jsonb, %s)
            """,
            (
                mapping_template_id,
                firm_id,
                client_id,
                "Bank Mapping",
                '["date","amount","account"]',
                "99b85f5f43f9c4e95f08f6524d8ef6f7fca95ab93ea4f30089f2a73b6f2f1d41",
            ),
        )
    db_conn.commit()

    source_file = api_client.post(
        f"/v1/runs/{run_id}/source-files",
        json={
            "file_kind": "bank",
            "filename": "bank.csv",
            "uri": "s3://bucket/bank.csv",
            "checksum_sha256": "a" * 64,
            "byte_size": 123,
            "mapping_template_id": mapping_template_id,
            "observed_headers": ["Posting Date", "Amount", "Narrative"],
        },
        headers=_headers(user),
    )
    assert source_file.status_code == 201, source_file.text

    blocked = api_client.post(f"/v1/runs/{run_id}/reconcile/bank", json={"payload": {}}, headers=_headers(user))
    assert blocked.status_code == 409

    with db_conn.cursor() as cur:
        cur.execute(
            """
            select count(*)::int as total
            from public.variances
            where run_id = %s and code = 'IMP-001' and status = 'open'
            """,
            (run_id,),
        )
        assert cur.fetchone()["total"] == 1
