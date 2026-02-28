from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import app
import apps.api.app.api.routes as routes


class FakeCursor:
    def __init__(self, conn: "FakeConn") -> None:
        self.conn = conn
        self._one: dict[str, Any] | None = None
        self._all: list[dict[str, Any]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, query: str, params: tuple[Any, ...] | None = None) -> None:
        params = params or tuple()
        q = " ".join(query.lower().split())
        self._one = None
        self._all = []
        state = self.conn.state

        def has_firm_access(firm_id: str) -> bool:
            return (firm_id, self.conn.user_id) in state["memberships"]

        if "insert into public.audit_events" in q:
            state["audits"].append(params)
            return

        if "insert into public.firms(id, name, slug)" in q:
            firm_id, name, slug = params
            state["firms"][firm_id] = {
                "id": firm_id,
                "name": name,
                "slug": slug,
                "created_at": datetime.now(timezone.utc),
            }
            return

        if "insert into public.firm_memberships" in q:
            firm_id, user_id = params
            state["memberships"][(str(firm_id), str(user_id))] = "owner"
            return

        if "select id, name, slug, created_at from public.firms" in q:
            firm_id = str(params[0])
            firm = state["firms"].get(firm_id)
            if firm and has_firm_access(firm_id):
                self._one = firm
            return

        if "select id from public.firms where id =" in q:
            firm_id = str(params[0])
            if firm_id in state["firms"] and has_firm_access(firm_id):
                self._one = {"id": firm_id}
            return

        if "insert into public.clients(firm_id, name, external_ref)" in q:
            firm_id, name, external_ref = params
            client_id = str(uuid4())
            client = {
                "id": client_id,
                "firm_id": str(firm_id),
                "name": name,
                "external_ref": external_ref,
                "created_at": datetime.now(timezone.utc),
            }
            state["clients"][client_id] = client
            self._one = client
            return

        if "insert into public.client_recon_policies" in q:
            client_id, firm_id = params
            cid = str(client_id)
            if cid not in state["policies"]:
                state["policies"][cid] = {
                    "client_id": cid,
                    "firm_id": str(firm_id),
                    "amount_tolerance": 0.01,
                    "date_window_days": 5,
                    "max_group_size": 5,
                    "enable_one_to_many": True,
                    "enable_many_to_one": True,
                    "require_allowed_account": True,
                    "updated_at": datetime.now(timezone.utc),
                }
            return

        if "select id from public.clients where id =" in q and "and firm_id =" in q:
            client_id, firm_id = map(str, params)
            client = state["clients"].get(client_id)
            if client and client["firm_id"] == firm_id and has_firm_access(firm_id):
                self._one = {"id": client_id}
            return

        if "insert into public.runs(firm_id, client_id, period_start, period_end, created_by)" in q:
            firm_id, client_id, period_start, period_end, created_by = params
            run_id = str(uuid4())
            run = {
                "id": run_id,
                "firm_id": str(firm_id),
                "client_id": str(client_id),
                "status": "draft",
                "period_start": period_start,
                "period_end": period_end,
                "created_at": datetime.now(timezone.utc),
                "created_by": str(created_by),
            }
            state["runs"][run_id] = run
            self._one = {
                "id": run_id,
                "firm_id": str(firm_id),
                "client_id": str(client_id),
                "status": "draft",
                "period_start": period_start,
                "period_end": period_end,
                "created_at": run["created_at"],
            }
            return

        if "select id, firm_id, client_id, status from public.runs" in q:
            run_id = str(params[0])
            run = state["runs"].get(run_id)
            if run and has_firm_access(run["firm_id"]):
                self._one = {
                    "id": run_id,
                    "firm_id": run["firm_id"],
                    "client_id": run["client_id"],
                    "status": run["status"],
                }
            return

        if "insert into public.source_files(" in q:
            (
                run_id,
                firm_id,
                client_id,
                file_kind,
                _filename,
                uri,
                checksum,
                _byte_size,
                _mapping_template_id,
                _registered_by,
            ) = params
            source_id = str(uuid4())
            row = {
                "id": source_id,
                "run_id": str(run_id),
                "file_kind": file_kind,
                "uri": uri,
                "checksum_sha256": checksum,
                "created_at": datetime.now(timezone.utc),
                "firm_id": str(firm_id),
                "client_id": str(client_id),
            }
            state["source_files"][source_id] = row
            self._one = {
                "id": source_id,
                "run_id": str(run_id),
                "file_kind": file_kind,
                "uri": uri,
                "checksum_sha256": checksum,
                "created_at": row["created_at"],
            }
            return

        if "select id, run_id, status, job_type, queued_at from public.jobs where run_id =" in q:
            run_id, idem_key = map(str, params)
            for job in state["jobs"].values():
                if job["run_id"] == run_id and job.get("idempotency_key") == idem_key:
                    self._one = {
                        "id": job["id"],
                        "run_id": job["run_id"],
                        "status": job["status"],
                        "job_type": job["job_type"],
                        "queued_at": job["queued_at"],
                    }
                    return
            return

        if "insert into public.jobs(" in q:
            run_id, firm_id, client_id, job_type, _payload, idem_key, _created_by = params
            job_id = str(uuid4())
            row = {
                "id": job_id,
                "run_id": str(run_id),
                "firm_id": str(firm_id),
                "client_id": str(client_id),
                "job_type": job_type,
                "status": "queued",
                "queued_at": datetime.now(timezone.utc),
                "idempotency_key": idem_key,
            }
            state["jobs"][job_id] = row
            self._one = {
                "id": job_id,
                "run_id": row["run_id"],
                "status": "queued",
                "job_type": row["job_type"],
                "queued_at": row["queued_at"],
            }
            return

        if "select r.id, r.status, r.firm_id, r.client_id," in q:
            run_id = str(params[0])
            run = state["runs"].get(run_id)
            if run and has_firm_access(run["firm_id"]):
                summary = state["summaries"].get(run_id, {})
                self._one = {
                    "id": run_id,
                    "status": run["status"],
                    "firm_id": run["firm_id"],
                    "client_id": run["client_id"],
                    "expected_net_pay": summary.get("expected_net_pay"),
                    "matched_bank_total": summary.get("matched_bank_total"),
                    "delta": summary.get("delta"),
                    "tieout_status": summary.get("status"),
                    "computed_at": summary.get("computed_at"),
                }
            return

        if "select severity, count(*) as total from public.variances" in q:
            run_id = str(params[0])
            counts: dict[str, int] = {}
            for variance in state["variances"]:
                if variance["run_id"] == run_id and variance["status"] == "open":
                    counts[variance["severity"]] = counts.get(variance["severity"], 0) + 1
            self._all = [{"severity": key, "total": value} for key, value in counts.items()]
            return

        if "select r.id, r.client_id," in q and "join public.client_recon_policies" in q:
            run_id = str(params[0])
            run = state["runs"].get(run_id)
            if run and has_firm_access(run["firm_id"]):
                policy = state["policies"].get(run["client_id"])
                if not policy:
                    return
                summary = state["summaries"].get(run_id, {})
                self._one = {
                    "id": run_id,
                    "client_id": run["client_id"],
                    "expected_net_pay": summary.get("expected_net_pay"),
                    "matched_bank_total": summary.get("matched_bank_total"),
                    "delta": summary.get("delta"),
                    "status": summary.get("status"),
                    "amount_tolerance": policy["amount_tolerance"],
                    "date_window_days": policy["date_window_days"],
                    "max_group_size": policy["max_group_size"],
                    "enable_one_to_many": policy["enable_one_to_many"],
                    "enable_many_to_one": policy["enable_many_to_one"],
                    "require_allowed_account": policy["require_allowed_account"],
                }
            return

        if "select id from public.runs where id =" in q:
            run_id = str(params[0])
            run = state["runs"].get(run_id)
            if run and has_firm_access(run["firm_id"]):
                self._one = {"id": run_id}
            return

        if "select id, code, severity, status, message, amount, event_date, account_ref, details" in q:
            run_id, category, status = params
            rows = [
                item
                for item in state["variances"]
                if item["run_id"] == str(run_id) and item["category"] == category and item["status"] == status
            ]
            rows.sort(key=lambda item: (item["code"], item["event_date"] or date.min, item["id"]))
            self._all = rows
            return

        if "select mg.id, mg.group_kind, mg.match_confidence" in q:
            run_id = str(params[0])
            rows = [item for item in state["match_groups"] if item["run_id"] == run_id]
            rows.sort(key=lambda item: (item["group_kind"], item["id"]))
            self._all = rows
            return

        if "update public.client_recon_policies" in q:
            (
                amount_tolerance,
                date_window_days,
                max_group_size,
                enable_one_to_many,
                enable_many_to_one,
                require_allowed_account,
                client_id,
            ) = params
            cid = str(client_id)
            policy = state["policies"].get(cid)
            if policy and has_firm_access(policy["firm_id"]):
                if amount_tolerance is not None:
                    policy["amount_tolerance"] = amount_tolerance
                if date_window_days is not None:
                    policy["date_window_days"] = date_window_days
                if max_group_size is not None:
                    policy["max_group_size"] = max_group_size
                if enable_one_to_many is not None:
                    policy["enable_one_to_many"] = enable_one_to_many
                if enable_many_to_one is not None:
                    policy["enable_many_to_one"] = enable_many_to_one
                if require_allowed_account is not None:
                    policy["require_allowed_account"] = require_allowed_account
                policy["updated_at"] = datetime.now(timezone.utc)
                self._one = policy.copy()
            return

        if "select id, firm_id from public.clients where id =" in q:
            client_id = str(params[0])
            client = state["clients"].get(client_id)
            if client and has_firm_access(client["firm_id"]):
                self._one = {"id": client_id, "firm_id": client["firm_id"]}
            return

        if "delete from public.client_bank_accounts where client_id =" in q:
            client_id = str(params[0])
            state["accounts"][client_id] = []
            return

        if "insert into public.client_bank_accounts" in q:
            client_id, _firm_id, account_ref, label = params
            row = {"id": str(uuid4()), "account_ref": account_ref, "label": label}
            state["accounts"].setdefault(str(client_id), []).append(row)
            return

        if "select id, account_ref, label from public.client_bank_accounts" in q:
            client_id = str(params[0])
            rows = sorted(state["accounts"].get(client_id, []), key=lambda item: item["account_ref"])
            self._all = rows
            return

        raise AssertionError(f"Unhandled query: {q}")

    def fetchone(self) -> dict[str, Any] | None:
        return self._one

    def fetchall(self) -> list[dict[str, Any]]:
        return self._all


class FakeConn:
    def __init__(self, user_id: str, state: dict[str, Any]) -> None:
        self.user_id = user_id
        self.state = state

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)


@pytest.fixture()
def client_and_state(monkeypatch: pytest.MonkeyPatch):
    state: dict[str, Any] = {
        "firms": {},
        "memberships": {},
        "clients": {},
        "policies": {},
        "runs": {},
        "source_files": {},
        "jobs": {},
        "variances": [],
        "summaries": {},
        "match_groups": [],
        "accounts": {},
        "audits": [],
    }

    @contextmanager
    def fake_db_session(user_id: str):
        yield FakeConn(user_id, state)

    monkeypatch.setattr(routes, "db_session", fake_db_session)

    with TestClient(app) as client:
        yield client, state


def _headers(user_id: str) -> dict[str, str]:
    return {"X-User-Id": user_id}


def test_health_and_missing_auth(client_and_state) -> None:
    client, _ = client_and_state
    assert client.get("/v1/health").status_code == 200
    assert client.post("/v1/firms", json={"name": "A", "slug": "a"}).status_code == 401


def test_full_route_flow_success(client_and_state) -> None:
    client, state = client_and_state
    user = "00000000-0000-0000-0000-000000000901"

    firm = client.post("/v1/firms", json={"name": "Firm", "slug": "firm"}, headers=_headers(user))
    assert firm.status_code == 201
    firm_id = firm.json()["id"]

    create_client = client.post(
        "/v1/clients",
        json={"firm_id": firm_id, "name": "Client", "external_ref": "X"},
        headers=_headers(user),
    )
    assert create_client.status_code == 201
    client_id = create_client.json()["id"]

    run = client.post(
        "/v1/runs",
        json={"firm_id": firm_id, "client_id": client_id, "period_start": "2025-01-01", "period_end": "2025-01-31"},
        headers=_headers(user),
    )
    assert run.status_code == 201
    run_id = run.json()["id"]

    source_file = client.post(
        f"/v1/runs/{run_id}/source-files",
        json={
            "file_kind": "bank",
            "filename": "bank.csv",
            "uri": "s3://bucket/bank.csv",
            "checksum_sha256": "a" * 64,
            "byte_size": 1,
        },
        headers=_headers(user),
    )
    assert source_file.status_code == 201

    job = client.post(
        f"/v1/runs/{run_id}/jobs",
        json={"job_type": "noop", "payload": {}, "idempotency_key": "idem-1"},
        headers=_headers(user),
    )
    assert job.status_code == 201

    same_job = client.post(
        f"/v1/runs/{run_id}/jobs",
        json={"job_type": "noop", "payload": {}, "idempotency_key": "idem-1"},
        headers=_headers(user),
    )
    assert same_job.status_code == 201
    assert same_job.json()["id"] == job.json()["id"]

    recon = client.post(
        f"/v1/runs/{run_id}/reconcile/bank",
        json={"idempotency_key": "recon-1", "payload": {}},
        headers=_headers(user),
    )
    assert recon.status_code == 201

    state["summaries"][run_id] = {
        "expected_net_pay": "10000.00",
        "matched_bank_total": "10000.00",
        "delta": "0.00",
        "status": "Tied",
        "computed_at": datetime.now(timezone.utc),
    }
    state["variances"] = [
        {
            "id": str(uuid4()),
            "run_id": run_id,
            "code": "BNK-001",
            "severity": "blocker",
            "status": "open",
            "category": "bank",
            "message": "missing",
            "amount": "10.00",
            "event_date": date(2025, 1, 31),
            "account_ref": None,
            "details": {},
        }
    ]
    state["match_groups"] = [
        {
            "id": str(uuid4()),
            "run_id": run_id,
            "group_kind": "one_to_one",
            "match_confidence": "deterministic",
            "expected_total": "10000.00",
            "bank_total": "10000.00",
            "delta": "0.00",
            "matched_on": date(2025, 1, 31),
            "members_count": 2,
        }
    ]

    summary = client.get(f"/v1/runs/{run_id}/summary", headers=_headers(user))
    assert summary.status_code == 200
    assert summary.json()["open_variances"]["blocker"] == 1

    bank_tieout = client.get(f"/v1/runs/{run_id}/bank-tieout", headers=_headers(user))
    assert bank_tieout.status_code == 200
    assert bank_tieout.json()["policy"]["date_window_days"] == 5

    variances = client.get(f"/v1/runs/{run_id}/variances?category=bank&status=open", headers=_headers(user))
    assert variances.status_code == 200
    assert len(variances.json()) == 1

    match_groups = client.get(f"/v1/runs/{run_id}/match-groups", headers=_headers(user))
    assert match_groups.status_code == 200
    assert len(match_groups.json()) == 1

    update_policy = client.patch(
        f"/v1/clients/{client_id}/recon-policy",
        json={"date_window_days": 7, "max_group_size": 3},
        headers=_headers(user),
    )
    assert update_policy.status_code == 200
    assert update_policy.json()["date_window_days"] == 7

    replace_accounts = client.put(
        f"/v1/clients/{client_id}/bank-accounts",
        json={"accounts": [{"account_ref": "ACCT-1", "label": "Payroll"}]},
        headers=_headers(user),
    )
    assert replace_accounts.status_code == 200
    assert replace_accounts.json()["accounts"][0]["account_ref"] == "ACCT-1"


def test_not_found_and_tenant_isolation(client_and_state) -> None:
    client, state = client_and_state
    owner = "00000000-0000-0000-0000-000000000902"
    outsider = "00000000-0000-0000-0000-000000000903"

    firm = client.post("/v1/firms", json={"name": "Firm2", "slug": "firm2"}, headers=_headers(owner))
    firm_id = firm.json()["id"]
    create_client = client.post(
        "/v1/clients",
        json={"firm_id": firm_id, "name": "Client2", "external_ref": None},
        headers=_headers(owner),
    )
    client_id = create_client.json()["id"]
    run = client.post(
        "/v1/runs",
        json={"firm_id": firm_id, "client_id": client_id, "period_start": "2025-01-01", "period_end": "2025-01-31"},
        headers=_headers(owner),
    )
    run_id = run.json()["id"]

    assert client.get(f"/v1/runs/{run_id}/summary", headers=_headers(outsider)).status_code == 404
    assert client.get(f"/v1/runs/{uuid4()}/summary", headers=_headers(owner)).status_code == 404
    assert client.get(f"/v1/runs/{uuid4()}/bank-tieout", headers=_headers(owner)).status_code == 404
    assert client.get(f"/v1/runs/{uuid4()}/variances?category=bank&status=open", headers=_headers(owner)).status_code == 404
    assert client.get(f"/v1/runs/{uuid4()}/match-groups", headers=_headers(owner)).status_code == 404

    assert (
        client.post(
            "/v1/clients",
            json={"firm_id": str(uuid4()), "name": "MissingFirm", "external_ref": None},
            headers=_headers(owner),
        ).status_code
        == 404
    )

    assert (
        client.post(
            "/v1/runs",
            json={"firm_id": firm_id, "client_id": str(uuid4()), "period_start": "2025-01-01", "period_end": "2025-01-31"},
            headers=_headers(owner),
        ).status_code
        == 404
    )

    assert (
        client.post(
            f"/v1/runs/{uuid4()}/source-files",
            json={
                "file_kind": "bank",
                "filename": "bank.csv",
                "uri": "s3://bucket/bank.csv",
                "checksum_sha256": "a" * 64,
                "byte_size": 1,
            },
            headers=_headers(owner),
        ).status_code
        == 404
    )

    assert (
        client.post(
            f"/v1/runs/{uuid4()}/jobs",
            json={"job_type": "noop", "payload": {}},
            headers=_headers(owner),
        ).status_code
        == 404
    )

    assert (
        client.post(
            f"/v1/runs/{uuid4()}/reconcile/bank",
            json={"payload": {}},
            headers=_headers(owner),
        ).status_code
        == 404
    )

    missing_client_id = str(uuid4())
    state["policies"].pop(missing_client_id, None)
    assert (
        client.patch(
            f"/v1/clients/{missing_client_id}/recon-policy",
            json={"date_window_days": 8},
            headers=_headers(owner),
        ).status_code
        == 404
    )

    assert (
        client.put(
            f"/v1/clients/{uuid4()}/bank-accounts",
            json={"accounts": [{"account_ref": "A", "label": None}]},
            headers=_headers(owner),
        ).status_code
        == 404
    )
