from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import apps.api.app.db as api_db
import apps.worker.main as worker
from libs.core.engine.types import GLTieOutSummary, MatchGroup, ReconPolicy, RunSummary, Variance


class QueueCursor:
    def __init__(self, conn: "QueueConn") -> None:
        self.conn = conn

    def __enter__(self) -> "QueueCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, query: str, params: tuple[Any, ...] | None = None) -> None:
        self.conn.queries.append((" ".join(query.lower().split()), params or tuple()))

    def fetchone(self):
        if self.conn.fetchone_queue:
            return self.conn.fetchone_queue.pop(0)
        return None

    def fetchall(self):
        if self.conn.fetchall_queue:
            return self.conn.fetchall_queue.pop(0)
        return []


class QueueConn:
    def __init__(self) -> None:
        self.queries: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchone_queue: list[Any] = []
        self.fetchall_queue: list[Any] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self) -> QueueCursor:
        return QueueCursor(self)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True

    @contextmanager
    def transaction(self):
        yield self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


@dataclass
class FakeBankResult:
    match_groups: list[MatchGroup]
    variances: list[Variance]
    summary: RunSummary


@dataclass
class FakeGLResult:
    variances: list[Variance]
    summary: GLTieOutSummary


class FakeStorage:
    def __init__(self) -> None:
        self.uploaded: list[tuple[str, str, bytes, str]] = []

    def upload_bytes(self, bucket: str, path: str, data: bytes, content_type: str) -> str:
        self.uploaded.append((bucket, path, data, content_type))
        return f"local://{bucket}/{path}"


def test_db_session_success_and_error(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = QueueConn()

    def fake_connect(_url: str, row_factory=None):
        return conn

    monkeypatch.setattr(api_db.Connection, "connect", fake_connect)

    with api_db.db_session("00000000-0000-0000-0000-000000000100"):
        pass
    assert conn.committed is True
    assert conn.closed is True

    conn2 = QueueConn()

    def fake_connect_2(_url: str, row_factory=None):
        return conn2

    monkeypatch.setattr(api_db.Connection, "connect", fake_connect_2)
    with pytest.raises(RuntimeError):
        with api_db.db_session("00000000-0000-0000-0000-000000000100"):
            raise RuntimeError("boom")
    assert conn2.rolled_back is True


def test_worker_loader_helpers() -> None:
    conn = QueueConn()
    conn.fetchone_queue.append(
        {
            "amount_tolerance": "0.02",
            "date_window_days": 6,
            "max_group_size": 3,
            "enable_one_to_many": True,
            "enable_many_to_one": False,
            "require_allowed_account": True,
        }
    )
    conn.fetchall_queue.append([{"account_ref": "ACCT-1"}, {"account_ref": "ACCT-2"}])
    policy, accounts = worker._load_policy(conn, "client-1")
    assert policy.amount_tolerance == Decimal("0.02")
    assert accounts == {"ACCT-1", "ACCT-2"}

    conn = QueueConn()
    conn.fetchone_queue.append(None)
    conn.fetchall_queue.append([])
    policy_default, accounts_default = worker._load_policy(conn, "client-2")
    assert policy_default.amount_tolerance == Decimal("0.01")
    assert accounts_default == set()

    conn = QueueConn()
    conn.fetchall_queue.append(
        [
            {
                "id": str(uuid4()),
                "payment_date": date(2025, 1, 31),
                "net_amount": "100.00",
                "employee_ref": "E1",
                "tax_amount": "10.00",
                "pension_amount": "5.00",
                "other_amount": "1.00",
            }
        ]
    )
    expected = worker._load_payroll_expected(conn, "run-1")
    assert expected[0].tax_amount == Decimal("10.00")

    conn = QueueConn()
    conn.fetchall_queue.append(
        [
            {
                "id": str(uuid4()),
                "posted_date": date(2025, 1, 31),
                "amount_signed": "-100.00",
                "account_ref": "ACCT-1",
                "description": "desc",
            }
        ]
    )
    bank = worker._load_bank_transactions(conn, "run-1")
    assert bank[0].withdrawal_abs == Decimal("100.00")

    conn = QueueConn()
    conn.fetchall_queue.append(
        [
            {
                "id": str(uuid4()),
                "entry_date": date(2025, 1, 31),
                "account_code": "2200",
                "debit_amount": "0.00",
                "credit_amount": "100.00",
                "description": "line",
            }
        ]
    )
    gl = worker._load_gl_journal_lines(conn, "run-1")
    assert gl[0].net_amount == Decimal("100.00")

    conn = QueueConn()
    conn.fetchall_queue.append([{"bucket": "net_pay_control", "account_code": "2200"}])
    buckets = worker._load_gl_bucket_accounts(conn, "client-1")
    assert buckets["net_pay_control"] == {"2200"}


def test_worker_run_meta_timing_and_sla_helpers() -> None:
    conn = QueueConn()
    conn.fetchone_queue.append({"id": "run-1", "firm_id": "firm-1", "client_id": "client-1", "period_start": date(2025, 1, 1), "period_end": date(2025, 1, 31)})
    run_meta = worker._load_run_meta(conn, "run-1")
    assert run_meta["period_end"] == date(2025, 1, 31)

    conn = QueueConn()
    conn.fetchone_queue.append(None)
    with pytest.raises(RuntimeError, match="Run not found"):
        worker._load_run_meta(conn, "run-missing")

    conn = QueueConn()
    conn.fetchone_queue.append(
        {
            "tax_due_day": 20,
            "pension_due_day": 21,
            "bacs_visibility_business_days": 2,
            "holiday_calendar": "GB",
            "enabled": False,
        }
    )
    timing = worker._load_uk_timing_policy(conn, "client-1")
    assert timing.tax_due_day == 20
    assert timing.enabled is False

    conn = QueueConn()
    conn.fetchone_queue.append(None)
    default_timing = worker._load_uk_timing_policy(conn, "client-2")
    assert default_timing.tax_due_day == 22
    assert default_timing.enabled is True

    assert worker._parse_as_of_date({"as_of_date": "2025-02-28"}) == date(2025, 2, 28)
    assert worker._parse_as_of_date({"as_of_date": date(2025, 2, 27)}) == date(2025, 2, 27)
    auto_date = worker._parse_as_of_date({})
    assert isinstance(auto_date, date)

    as_of = date(2025, 2, 28)
    assert worker._sla_state(as_of, None) is None
    assert worker._sla_state(as_of, date(2025, 2, 27)) == "overdue"
    assert worker._sla_state(as_of, date(2025, 3, 1)) == "due_soon"
    assert worker._sla_state(as_of, date(2025, 3, 20)) == "on_track"


def test_persist_and_export_helpers() -> None:
    conn = QueueConn()
    group = MatchGroup(
        id=str(uuid4()),
        group_kind="one_to_one",
        match_confidence="deterministic",
        expected_ids=[str(uuid4())],
        bank_ids=[str(uuid4())],
        expected_total=Decimal("100.00"),
        bank_total=Decimal("100.00"),
        delta=Decimal("0.00"),
        matched_on=date(2025, 1, 31),
    )
    variance = Variance(
        id=str(uuid4()),
        code="BNK-001",
        category="bank",
        severity="blocker",
        status="open",
        message="missing",
        amount=Decimal("100.00"),
    )
    bank_result = FakeBankResult(
        match_groups=[group],
        variances=[variance],
        summary=RunSummary(Decimal("100.00"), Decimal("100.00"), Decimal("0.00"), "Tied"),
    )
    worker._persist_bank_result(
        conn,
        run_id="run-1",
        firm_id="firm-1",
        client_id="client-1",
        policy=ReconPolicy(),
        result=bank_result,
        actor_user_id="00000000-0000-0000-0000-000000000101",
    )
    assert any("insert into public.match_groups" in query for query, _ in conn.queries)

    conn = QueueConn()
    gl_variance = Variance(
        id=str(uuid4()),
        code="GL-001",
        category="gl",
        severity="blocker",
        status="open",
        message="missing",
    )
    gl_result = FakeGLResult(
        variances=[gl_variance],
        summary=GLTieOutSummary(
            payroll_totals={"net_pay_control": Decimal("100.00"), "taxes": Decimal("0.00"), "pension": Decimal("0.00"), "other": Decimal("0.00")},
            gl_totals={"net_pay_control": Decimal("100.00"), "taxes": Decimal("0.00"), "pension": Decimal("0.00"), "other": Decimal("0.00")},
            deltas={"net_pay_control": Decimal("0.00"), "taxes": Decimal("0.00"), "pension": Decimal("0.00"), "other": Decimal("0.00")},
            is_balanced=True,
            status="Tied",
            rules_used={"checks": []},
        ),
    )
    worker._persist_gl_result(
        conn,
        run_id="run-2",
        firm_id="firm-2",
        client_id="client-2",
        result=gl_result,
        actor_user_id="00000000-0000-0000-0000-000000000102",
    )
    assert any("insert into public.run_gl_tieout_summaries" in query for query, _ in conn.queries)

    conn = QueueConn()
    conn.fetchone_queue.append({"id": str(uuid4())})
    pack_id = worker._upsert_export_pack(
        conn,
        run_id="run-3",
        firm_id="firm-3",
        client_id="client-3",
        status_value="generated",
        pack_hash="abc",
        storage_bucket="audit-packs",
        storage_path="runs/a.zip",
        manifest={"a": 1},
        error=None,
    )
    assert pack_id

    conn = QueueConn()
    conn.fetchone_queue.extend([None, {"id": str(uuid4())}])
    fallback_pack_id = worker._upsert_export_pack(
        conn,
        run_id="run-4",
        firm_id="firm-4",
        client_id="client-4",
        status_value="failed",
        pack_hash=None,
        storage_bucket=None,
        storage_path=None,
        manifest=None,
        error={"message": "x"},
    )
    assert fallback_pack_id


def test_worker_connect_and_claim_next_job(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = QueueConn()
    conn.fetchone_queue.append({"id": "job-1"})
    monkeypatch.setattr(worker.Connection, "connect", lambda _url, row_factory=None: conn)
    assert worker._connect() is conn
    assert worker.claim_next_job(conn) == {"id": "job-1"}


def test_load_run_export_inputs_paths() -> None:
    conn = QueueConn()
    conn.fetchone_queue.append(None)
    with pytest.raises(RuntimeError, match="Run not found for export"):
        worker._load_run_export_inputs(conn, "run-missing")

    conn = QueueConn()
    now = datetime.now(timezone.utc)
    conn.fetchone_queue.extend(
        [
            {
                "id": "run-1",
                "run_status": "approved",
                "expected_net_pay": "100.00",
                "matched_bank_total": "100.00",
                "delta": "0.00",
                "bank_status": "Tied",
                "bank_computed_at": now,
                "gl_status": "Tied",
                "payroll_totals": {"net_pay_control": "100.00"},
                "gl_totals": {"net_pay_control": "100.00"},
                "deltas": {"net_pay_control": "0.00"},
                "rules_used": {"checks": []},
                "gl_computed_at": now,
            },
            {"status": "approved", "prepared_by": "u1", "prepared_at": now, "reviewer_id": "u2", "reviewed_at": now},
        ]
    )
    conn.fetchall_queue.extend(
        [
            [
                {
                    "id": "v-1",
                    "code": "GL-001",
                    "category": "gl",
                    "severity": "blocker",
                    "status": "open",
                    "message": "missing",
                    "amount": "100.00",
                    "event_date": None,
                    "account_ref": None,
                    "note": None,
                    "changed_by": None,
                    "changed_at": None,
                }
            ],
            [{"id": "a-1", "event_type": "run.approved", "actor_user_id": "u2", "created_at": now, "entity_type": "run", "entity_id": "run-1"}],
            [{"id": "s-1", "file_kind": "bank", "checksum_sha256": "a" * 64, "created_at": now, "uri": "s3://x"}],
        ]
    )
    summary, gl_summary, variances, audit_rows, source_files, signoff = worker._load_run_export_inputs(conn, "run-1")
    assert summary["run_status"] == "approved"
    assert gl_summary is not None
    assert len(variances) == 1
    assert len(audit_rows) == 1
    assert len(source_files) == 1
    assert signoff["status"] == "approved"


def test_mark_job_complete_and_locked_job_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = QueueConn()
    worker._mark_job_complete(conn, "job-1", {"ok": True})
    assert any("set status = 'succeeded'" in query for query, _ in conn.queries)

    monkeypatch.setattr(worker, "_run_locked", lambda *_args, **_kwargs: True)
    locked_job = {"id": "job-lock", "run_id": "run-1", "firm_id": "firm-1", "client_id": "client-1", "created_by": None}

    with pytest.raises(PermissionError):
        worker.process_job(QueueConn(), {"job_type": "noop", **locked_job})

    with pytest.raises(PermissionError):
        worker.process_job(QueueConn(), {"job_type": "reconcile_bank", **locked_job})

    with pytest.raises(PermissionError):
        worker.process_job(QueueConn(), {"job_type": "reconcile_gl", **locked_job})


def test_process_job_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, int] = {}

    def bump(name: str):
        calls[name] = calls.get(name, 0) + 1

    monkeypatch.setattr(worker, "_mark_job_complete", lambda *_args, **_kwargs: bump("complete"))
    monkeypatch.setattr(worker, "_audit", lambda *_args, **_kwargs: bump("audit"))

    # noop branch
    conn = QueueConn()
    conn.fetchone_queue.append({"locked_at": None})
    worker.process_job(
        conn,
        {"id": "job-1", "job_type": "noop", "run_id": "run-1", "firm_id": "firm-1", "client_id": "client-1", "created_by": None},
    )

    # unsupported branch
    worker.process_job(
        QueueConn(),
        {"id": "job-2", "job_type": "ingest", "run_id": "run-1", "firm_id": "firm-1", "client_id": "client-1", "created_by": None},
    )

    # reconcile_bank branch
    monkeypatch.setattr(worker, "_run_locked", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(worker, "_load_policy", lambda *_args, **_kwargs: (ReconPolicy(), {"ACCT-1"}))
    monkeypatch.setattr(
        worker,
        "_load_run_meta",
        lambda *_args, **_kwargs: {
            "id": "run-2",
            "firm_id": "firm-2",
            "client_id": "client-2",
            "period_start": date(2025, 1, 1),
            "period_end": date(2025, 1, 31),
        },
    )
    monkeypatch.setattr(worker, "_load_payroll_expected", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(worker, "_load_bank_transactions", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(worker, "reconcile_bank", lambda **_kwargs: FakeBankResult([], [], RunSummary(Decimal("0.00"), Decimal("0.00"), Decimal("0.00"), "Tied")))
    monkeypatch.setattr(worker, "_persist_bank_result", lambda *_args, **_kwargs: bump("persist_bank"))
    worker.process_job(
        QueueConn(),
        {"id": "job-3", "job_type": "reconcile_bank", "run_id": "run-2", "firm_id": "firm-2", "client_id": "client-2", "created_by": None},
    )

    # reconcile_gl branch
    monkeypatch.setattr(worker, "_load_gl_journal_lines", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(worker, "_load_gl_bucket_accounts", lambda *_args, **_kwargs: {"net_pay_control": set(), "taxes": set(), "pension": set(), "other": set()})
    monkeypatch.setattr(
        worker,
        "reconcile_gl",
        lambda **_kwargs: FakeGLResult(
            [],
            GLTieOutSummary(
                payroll_totals={"net_pay_control": Decimal("0.00"), "taxes": Decimal("0.00"), "pension": Decimal("0.00"), "other": Decimal("0.00")},
                gl_totals={"net_pay_control": Decimal("0.00"), "taxes": Decimal("0.00"), "pension": Decimal("0.00"), "other": Decimal("0.00")},
                deltas={"net_pay_control": Decimal("0.00"), "taxes": Decimal("0.00"), "pension": Decimal("0.00"), "other": Decimal("0.00")},
                is_balanced=True,
                status="Tied",
                rules_used={"checks": []},
            ),
        ),
    )
    monkeypatch.setattr(worker, "_persist_gl_result", lambda *_args, **_kwargs: bump("persist_gl"))
    worker.process_job(
        QueueConn(),
        {"id": "job-4", "job_type": "reconcile_gl", "run_id": "run-3", "firm_id": "firm-3", "client_id": "client-3", "created_by": None},
    )

    # export branch
    monkeypatch.setattr(worker, "_load_run_export_inputs", lambda *_args, **_kwargs: ({"run_status": "approved", "tieout": {}}, None, [], [], [], {}))
    monkeypatch.setattr(worker, "build_export_pack", lambda **_kwargs: (b"zip", "hash", {"manifest": True}))
    monkeypatch.setattr(worker, "get_storage_client", lambda: FakeStorage())
    monkeypatch.setattr(worker, "_upsert_export_pack", lambda *_args, **_kwargs: "pack-1")
    worker.process_job(
        QueueConn(),
        {"id": "job-5", "job_type": "export_pack", "run_id": "run-4", "firm_id": "firm-4", "client_id": "client-4", "created_by": None},
    )

    assert calls["complete"] >= 5
    assert calls["persist_bank"] == 1
    assert calls["persist_gl"] == 1


def test_mark_job_failed_retry_and_final() -> None:
    conn = QueueConn()
    worker._mark_job_failed(
        conn,
        {
            "id": "job-1",
            "run_id": "run-1",
            "firm_id": "firm-1",
            "client_id": "client-1",
            "job_type": "export_pack",
            "attempt_count": 1,
            "max_attempts": 3,
            "created_by": None,
        },
        RuntimeError("x"),
    )
    assert any("set status = 'queued'" in query for query, _ in conn.queries)

    conn = QueueConn()
    worker._mark_job_failed(
        conn,
        {
            "id": "job-2",
            "run_id": "run-2",
            "firm_id": "firm-2",
            "client_id": "client-2",
            "job_type": "reconcile_gl",
            "attempt_count": 3,
            "max_attempts": 3,
            "created_by": None,
        },
        RuntimeError("x"),
    )
    assert any("set status = 'failed'" in query for query, _ in conn.queries)
    assert any("update public.runs set status = 'failed'" in query for query, _ in conn.queries)

    conn = QueueConn()
    worker._mark_job_failed(
        conn,
        {
            "id": "job-3",
            "run_id": "run-3",
            "firm_id": "firm-3",
            "client_id": "client-3",
            "job_type": "export_pack",
            "attempt_count": 3,
            "max_attempts": 3,
            "created_by": None,
        },
        RuntimeError("x"),
    )
    assert any("set status = 'failed'" in query for query, _ in conn.queries)
    assert not any("update public.runs set status = 'failed'" in query for query, _ in conn.queries)


def test_run_once_and_main(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = QueueConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(worker, "_connect", fake_connect)
    monkeypatch.setattr(worker, "claim_next_job", lambda _conn: None)
    assert worker.run_once() is False

    conn = QueueConn()

    @contextmanager
    def fake_connect2():
        yield conn

    monkeypatch.setattr(worker, "_connect", fake_connect2)
    monkeypatch.setattr(worker, "claim_next_job", lambda _conn: {"id": "job-1"})
    monkeypatch.setattr(worker, "process_job", lambda _conn, _job: None)
    assert worker.run_once() is True

    conn = QueueConn()

    @contextmanager
    def fake_connect3():
        yield conn

    marked: list[str] = []
    monkeypatch.setattr(worker, "_connect", fake_connect3)
    monkeypatch.setattr(worker, "claim_next_job", lambda _conn: {"id": "job-2", "run_id": "run", "firm_id": "firm", "client_id": "client", "job_type": "export_pack"})
    monkeypatch.setattr(worker, "process_job", lambda _conn, _job: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(worker, "_upsert_export_pack", lambda *_args, **_kwargs: "pack")
    monkeypatch.setattr(worker, "_mark_job_failed", lambda *_args, **_kwargs: marked.append("failed"))
    assert worker.run_once() is True
    assert marked == ["failed"]

    conn = QueueConn()

    @contextmanager
    def fake_connect4():
        yield conn

    monkeypatch.setattr(worker, "_connect", fake_connect4)
    monkeypatch.setattr(
        worker,
        "claim_next_job",
        lambda _conn: {"id": "job-3", "run_id": "run", "firm_id": "firm", "client_id": "client", "job_type": "export_pack"},
    )
    monkeypatch.setattr(worker, "process_job", lambda _conn, _job: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(worker, "_upsert_export_pack", lambda *_args, **_kwargs: "pack")
    monkeypatch.setattr(worker, "_mark_job_failed", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom2")))
    assert worker.run_once() is True
    assert conn.rolled_back is True

    monkeypatch.setattr(worker.argparse.ArgumentParser, "parse_args", lambda self: SimpleNamespace(once=True, poll_interval=0.0))
    called = {"count": 0}
    monkeypatch.setattr(worker, "run_once", lambda: called.__setitem__("count", called["count"] + 1) or False)
    worker.main()
    assert called["count"] == 1

    monkeypatch.setattr(worker.argparse.ArgumentParser, "parse_args", lambda self: SimpleNamespace(once=False, poll_interval=0.0))
    calls = {"count": 0}

    def run_once_loop():
        calls["count"] += 1
        return False

    def stop_sleep(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(worker, "run_once", run_once_loop)
    monkeypatch.setattr(worker.time, "sleep", stop_sleep)
    with pytest.raises(KeyboardInterrupt):
        worker.main()
    assert calls["count"] == 1
