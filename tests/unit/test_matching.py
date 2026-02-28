from __future__ import annotations

from datetime import date
from decimal import Decimal

from libs.core.engine import BankTransaction, PayrollExpected, ReconPolicy, reconcile_bank


def test_exact_match_tied() -> None:
    result = reconcile_bank(
        run_id="run-1",
        expected_items=[PayrollExpected(id="exp-1", payment_date=date(2025, 1, 31), net_amount=Decimal("10000.00"))],
        bank_items=[BankTransaction(id="bnk-1", posted_date=date(2025, 1, 31), amount_signed=Decimal("-10000.00"), account_ref="A1")],
        policy=ReconPolicy(),
        allowed_accounts={"A1"},
    )
    assert result.summary.status == "Tied"
    assert len(result.match_groups) == 1
    assert not result.variances


def test_split_match_tied() -> None:
    result = reconcile_bank(
        run_id="run-2",
        expected_items=[PayrollExpected(id="exp-1", payment_date=date(2025, 1, 31), net_amount=Decimal("10000.00"))],
        bank_items=[
            BankTransaction(id="bnk-1", posted_date=date(2025, 1, 31), amount_signed=Decimal("-6000.00"), account_ref="A1"),
            BankTransaction(id="bnk-2", posted_date=date(2025, 1, 31), amount_signed=Decimal("-4000.00"), account_ref="A1"),
        ],
        policy=ReconPolicy(max_group_size=5),
        allowed_accounts={"A1"},
    )
    assert result.summary.status == "Tied"
    assert result.match_groups[0].group_kind == "one_to_many"


def test_batch_match_many_to_one() -> None:
    result = reconcile_bank(
        run_id="run-3",
        expected_items=[
            PayrollExpected(id="exp-1", payment_date=date(2025, 1, 31), net_amount=Decimal("6000.00")),
            PayrollExpected(id="exp-2", payment_date=date(2025, 1, 31), net_amount=Decimal("4000.00")),
        ],
        bank_items=[BankTransaction(id="bnk-1", posted_date=date(2025, 1, 31), amount_signed=Decimal("-10000.00"), account_ref="A1")],
        policy=ReconPolicy(max_group_size=5),
        allowed_accounts={"A1"},
    )
    assert result.summary.status == "Tied"
    assert result.match_groups[0].group_kind == "many_to_one"


def test_ambiguous_candidates_need_review() -> None:
    result = reconcile_bank(
        run_id="run-4",
        expected_items=[PayrollExpected(id="exp-1", payment_date=date(2025, 1, 31), net_amount=Decimal("100.00"))],
        bank_items=[
            BankTransaction(id="bnk-1", posted_date=date(2025, 1, 31), amount_signed=Decimal("-100.00"), account_ref="A1"),
            BankTransaction(id="bnk-2", posted_date=date(2025, 1, 31), amount_signed=Decimal("-100.00"), account_ref="A2"),
        ],
        policy=ReconPolicy(),
        allowed_accounts={"A1", "A2"},
    )
    codes = {item.code for item in result.variances}
    assert "AMB-001" in codes
    assert result.summary.status == "Needs review"
