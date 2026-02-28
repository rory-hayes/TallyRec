from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import apps.api.app.db as api_db
import apps.worker.main as worker
from libs.core.engine.types import MatchGroup, ReconPolicy, RunSummary, Variance


class QueueCursor:
    def __init__(self, conn: "QueueConn") -> None:
        self.conn = conn
        self._one = None
        self._all = []

    def __enter__(self) -> "QueueCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, query: str, params: tuple[Any, ...] | None = None) -> None:
        params = params or tuple()
        self.conn.queries.append((" ".join(query.lower().split()), params))
        self._one = None
        self._all = []

    def fetchone(self):
        if self.conn.fetchone_queue:
            return self.conn.fetchone_queue.pop(0)
        return self._one

    def fetchall(self):
        if self.conn.fetchall_queue:
            return self.conn.fetchall_queue.pop(0)
        return self._all


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

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


@dataclass
class FakeReconResult:
    match_groups: list[MatchGroup]
    variances: list[Variance]
    summary: RunSummary


def test_db_session_success_and_error(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = QueueConn()

    def fake_connect(_url: str, row_factory=None):
        return conn

    monkeypatch.setattr(api_db.Connection, "connect", fake_connect)

    with api_db.db_session("00000000-0000-0000-0000-000000000100") as _session:
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
    assert conn2.closed is True


def test_claim_load_helpers_and_markers() -> None:
    conn = QueueConn()
    job_id = str(uuid4())
    conn.fetchone_queue.append({"id": job_id, "run_id": str(uuid4()), "status": "running"})
    claimed = worker.claim_next_job(conn)
    assert claimed and claimed["id"] == job_id

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
    assert policy.enable_many_to_one is False
    assert accounts == {"ACCT-1", "ACCT-2"}

    conn = QueueConn()
    conn.fetchone_queue.append(None)
    conn.fetchall_queue.append([])
    default_policy, default_accounts = worker._load_policy(conn, "client-2")
    assert default_policy.amount_tolerance == Decimal("0.01")
    assert default_accounts == set()

    conn = QueueConn()
    conn.fetchall_queue.append(
        [
            {"id": str(uuid4()), "payment_date": date(2025, 1, 31), "net_amount": "10.00", "employee_ref": "E1"},
        ]
    )
    conn.fetchall_queue.append(
        [
            {
                "id": str(uuid4()),
                "posted_date": date(2025, 1, 31),
                "amount_signed": "-10.00",
                "account_ref": "ACCT-1",
                "description": "desc",
            }
        ]
    )
    expected, bank = worker._load_canonical(conn, "run-1")
    assert expected[0].employee_ref == "E1"
    assert bank[0].withdrawal_abs == Decimal("10.00")

    conn = QueueConn()
    worker._mark_job_complete(conn, "job-1", {"ok": True})
    assert any("update public.jobs" in query for query, _ in conn.queries)

    conn = QueueConn()
    worker._mark_job_failed(
        conn,
        {
            "id": "job-2",
            "run_id": "run-2",
            "firm_id": "firm-2",
            "client_id": "client-2",
            "job_type": "noop",
            "created_by": None,
        },
        ValueError("bad"),
    )
    assert any("update public.runs set status = 'failed'" in query for query, _ in conn.queries)


def test_persist_result_and_process_job_paths(monkeypatch: pytest.MonkeyPatch) -> None:
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
        event_date=date(2025, 1, 31),
    )
    result = FakeReconResult(
        match_groups=[group],
        variances=[variance],
        summary=RunSummary(
            expected_net_pay=Decimal("100.00"),
            matched_bank_total=Decimal("100.00"),
            delta=Decimal("0.00"),
            status="Tied",
        ),
    )

    worker._persist_result(
        conn,
        run_id="run-1",
        firm_id="firm-1",
        client_id="client-1",
        policy=ReconPolicy(),
        result=result,
        actor_user_id="00000000-0000-0000-0000-000000000100",
    )
    assert any("insert into public.match_groups" in query for query, _ in conn.queries)
    assert any("insert into public.variances" in query for query, _ in conn.queries)

    calls: dict[str, Any] = {}

    def remember(name: str):
        def _inner(*args, **kwargs):
            calls.setdefault(name, []).append((args, kwargs))
        return _inner

    monkeypatch.setattr(worker, "_mark_job_complete", remember("complete"))
    monkeypatch.setattr(worker, "_persist_result", remember("persist"))
    monkeypatch.setattr(worker, "_audit", remember("audit"))
    monkeypatch.setattr(worker, "_load_policy", lambda _conn, _client_id: (ReconPolicy(), {"ACCT-1"}))
    monkeypatch.setattr(worker, "_load_canonical", lambda _conn, _run_id: ([], []))
    monkeypatch.setattr(
        worker,
        "reconcile_bank",
        lambda **kwargs: FakeReconResult(match_groups=[], variances=[], summary=RunSummary(Decimal("0.00"), Decimal("0.00"), Decimal("0.00"), "Tied")),
    )

    worker.process_job(
        QueueConn(),
        {
            "id": "job-3",
            "job_type": "reconcile_bank",
            "run_id": "run-3",
            "firm_id": "firm-3",
            "client_id": "client-3",
            "created_by": "00000000-0000-0000-0000-000000000100",
        },
    )
    assert calls["persist"]
    assert calls["complete"]

    calls.clear()
    noop_conn = QueueConn()
    worker.process_job(
        noop_conn,
        {
            "id": "job-4",
            "job_type": "noop",
            "run_id": "run-4",
            "firm_id": "firm-4",
            "client_id": "client-4",
            "created_by": None,
        },
    )
    assert calls["complete"]

    calls.clear()
    worker.process_job(
        QueueConn(),
        {
            "id": "job-5",
            "job_type": "ingest",
            "run_id": "run-5",
            "firm_id": "firm-5",
            "client_id": "client-5",
            "created_by": None,
        },
    )
    assert calls["complete"]


def test_run_once_and_main_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = QueueConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(worker, "_connect", fake_connect)
    monkeypatch.setattr(worker, "claim_next_job", lambda _conn: None)
    assert worker.run_once() is False
    assert conn.committed is True

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
    monkeypatch.setattr(worker, "claim_next_job", lambda _conn: {"id": "job-2", "run_id": "r", "firm_id": "f", "client_id": "c", "job_type": "noop"})
    monkeypatch.setattr(worker, "process_job", lambda _conn, _job: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(worker, "_mark_job_failed", lambda _conn, _job, _exc: marked.append("failed"))
    assert worker.run_once() is False
    assert marked == ["failed"]

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
