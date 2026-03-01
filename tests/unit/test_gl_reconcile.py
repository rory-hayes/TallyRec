from __future__ import annotations

from datetime import date
from decimal import Decimal

from libs.core.engine import GLJournalLine, PayrollExpected, reconcile_gl


def test_gl_balanced_and_matching_totals_tied() -> None:
    result = reconcile_gl(
        run_id="run-gl-1",
        payroll_expected=[
            PayrollExpected(
                id="exp-1",
                payment_date=date(2025, 1, 31),
                net_amount=Decimal("100.00"),
                tax_amount=Decimal("20.00"),
                pension_amount=Decimal("10.00"),
                other_amount=Decimal("5.00"),
            )
        ],
        gl_lines=[
            GLJournalLine(id="gl-1", entry_date=date(2025, 1, 31), account_code="2200", debit_amount=Decimal("0.00"), credit_amount=Decimal("100.00")),
            GLJournalLine(id="gl-2", entry_date=date(2025, 1, 31), account_code="2210", debit_amount=Decimal("0.00"), credit_amount=Decimal("20.00")),
            GLJournalLine(id="gl-3", entry_date=date(2025, 1, 31), account_code="2220", debit_amount=Decimal("0.00"), credit_amount=Decimal("10.00")),
            GLJournalLine(id="gl-4", entry_date=date(2025, 1, 31), account_code="2230", debit_amount=Decimal("0.00"), credit_amount=Decimal("5.00")),
            GLJournalLine(id="gl-5", entry_date=date(2025, 1, 31), account_code="1000", debit_amount=Decimal("135.00"), credit_amount=Decimal("0.00")),
        ],
        bucket_accounts={
            "net_pay_control": {"2200"},
            "taxes": {"2210"},
            "pension": {"2220"},
            "other": {"2230"},
        },
    )
    assert result.summary.is_balanced is True
    assert result.summary.status == "Tied"
    assert result.variances == []


def test_gl_net_pay_control_mismatch_variance() -> None:
    result = reconcile_gl(
        run_id="run-gl-2",
        payroll_expected=[
            PayrollExpected(id="exp-1", payment_date=date(2025, 1, 31), net_amount=Decimal("100.00"))
        ],
        gl_lines=[
            GLJournalLine(id="gl-1", entry_date=date(2025, 1, 31), account_code="2200", debit_amount=Decimal("0.00"), credit_amount=Decimal("90.00")),
            GLJournalLine(id="gl-2", entry_date=date(2025, 1, 31), account_code="1000", debit_amount=Decimal("90.00"), credit_amount=Decimal("0.00")),
        ],
        bucket_accounts={"net_pay_control": {"2200"}, "taxes": set(), "pension": set(), "other": set()},
    )
    codes = {item.code for item in result.variances}
    assert "GL-011" in codes
    assert result.summary.status == "Not tied"


def test_gl_missing_journal_variance() -> None:
    result = reconcile_gl(
        run_id="run-gl-3",
        payroll_expected=[PayrollExpected(id="exp-1", payment_date=date(2025, 1, 31), net_amount=Decimal("100.00"))],
        gl_lines=[],
        bucket_accounts={"net_pay_control": {"2200"}, "taxes": set(), "pension": set(), "other": set()},
    )
    codes = {item.code for item in result.variances}
    assert "GL-001" in codes
    assert result.summary.status == "Not tied"
