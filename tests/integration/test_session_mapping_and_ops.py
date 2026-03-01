from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg import Connection

pytestmark = pytest.mark.usefixtures("clean_db")


def _headers(user_id: str) -> dict[str, str]:
    return {"X-User-Id": user_id}


def _create_client(api_client: TestClient, user_id: str, firm_id: str, name: str) -> str:
    response = api_client.post(
        "/v1/clients",
        json={"firm_id": firm_id, "name": name, "external_ref": f"ext-{name}"},
        headers=_headers(user_id),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _create_run(api_client: TestClient, user_id: str, firm_id: str, client_id: str) -> str:
    response = api_client.post(
        "/v1/runs",
        json={"firm_id": firm_id, "client_id": client_id, "period_start": "2025-01-01", "period_end": "2025-01-31"},
        headers=_headers(user_id),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_session_listing_and_queue_stats(api_client: TestClient) -> None:
    user = "00000000-0000-0000-0000-00000000e101"
    firm = api_client.post("/v1/firms", json={"name": "Ops Firm", "slug": f"ops-{uuid4().hex[:6]}"}, headers=_headers(user))
    assert firm.status_code == 201, firm.text
    firm_id = firm.json()["id"]
    client_id = _create_client(api_client, user, firm_id, "Ops Client")
    run_id = _create_run(api_client, user, firm_id, client_id)

    session = api_client.get("/v1/session", headers=_headers(user))
    assert session.status_code == 200
    payload = session.json()
    assert payload["default_firm_id"] == firm_id
    assert len(payload["memberships"]) == 1
    assert payload["memberships"][0]["role"] == "owner"

    firms = api_client.get("/v1/firms", headers=_headers(user))
    assert firms.status_code == 200
    assert len(firms.json()) == 1

    clients = api_client.get(f"/v1/firms/{firm_id}/clients", headers=_headers(user))
    assert clients.status_code == 200
    assert clients.json()["clients"][0]["id"] == client_id

    runs = api_client.get(f"/v1/runs?firm_id={firm_id}", headers=_headers(user))
    assert runs.status_code == 200
    assert runs.json()["runs"][0]["id"] == run_id

    enqueue = api_client.post(f"/v1/runs/{run_id}/jobs", json={"job_type": "noop", "payload": {}}, headers=_headers(user))
    assert enqueue.status_code == 201, enqueue.text

    queue = api_client.get("/v1/ops/queue-stats", headers=_headers(user))
    assert queue.status_code == 200, queue.text
    assert queue.json()["counts"]["queued"] >= 1


def test_mapping_template_and_remap_clears_drift_blocker(api_client: TestClient, db_conn: Connection) -> None:
    user = "00000000-0000-0000-0000-00000000e201"
    firm = api_client.post("/v1/firms", json={"name": "Map Firm", "slug": f"map-{uuid4().hex[:6]}"}, headers=_headers(user))
    assert firm.status_code == 201, firm.text
    firm_id = firm.json()["id"]
    client_id = _create_client(api_client, user, firm_id, "Map Client")
    run_id = _create_run(api_client, user, firm_id, client_id)

    template = api_client.post(
        f"/v1/clients/{client_id}/mapping-templates",
        json={
            "name": "Bank Mapping",
            "file_kind": "bank",
            "mapping": {"date": "date", "amount": "amount"},
            "expected_headers": ["date", "amount", "account"],
        },
        headers=_headers(user),
    )
    assert template.status_code == 201, template.text
    template_id = template.json()["id"]

    source = api_client.post(
        f"/v1/runs/{run_id}/source-files",
        json={
            "file_kind": "bank",
            "filename": "bank.csv",
            "uri": "s3://bucket/bank.csv",
            "checksum_sha256": "a" * 64,
            "byte_size": 123,
            "mapping_template_id": template_id,
            "observed_headers": ["posted_on", "amount", "acct"],
        },
        headers=_headers(user),
    )
    assert source.status_code == 201, source.text
    source_file_id = source.json()["id"]

    blocked = api_client.post(f"/v1/runs/{run_id}/reconcile/bank", json={"payload": {}}, headers=_headers(user))
    assert blocked.status_code == 409

    remap = api_client.post(
        f"/v1/source-files/{source_file_id}/remap",
        json={
            "mapping_template_id": template_id,
            "observed_headers": ["date", "amount", "account"],
        },
        headers=_headers(user),
    )
    assert remap.status_code == 200, remap.text
    assert remap.json()["import_validation_status"] == "validated"

    files = api_client.get(f"/v1/runs/{run_id}/source-files", headers=_headers(user))
    assert files.status_code == 200
    assert files.json()[0]["import_validation_status"] == "validated"

    allowed = api_client.post(f"/v1/runs/{run_id}/reconcile/bank", json={"payload": {}}, headers=_headers(user))
    assert allowed.status_code == 201, allowed.text

    with db_conn.cursor() as cur:
        cur.execute(
            """
            select count(*)::int as total
            from public.variances
            where run_id = %s and code = 'IMP-001' and status = 'open'
            """,
            (run_id,),
        )
        assert cur.fetchone()["total"] == 0
