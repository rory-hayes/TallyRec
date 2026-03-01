from __future__ import annotations

from datetime import date
from decimal import Decimal

from libs.core.engine import PayrollExpected, UKTimingPolicy, add_business_days, build_timing_variances, compute_due_date


def _payroll_item(tax: str, pension: str) -> PayrollExpected:
    return PayrollExpected(
        id="exp-1",
        payment_date=date(2025, 1, 31),
        net_amount=Decimal("100.00"),
        tax_amount=Decimal(tax),
        pension_amount=Decimal(pension),
        other_amount=Decimal("0.00"),
    )


def test_expected_later_within_visibility_window() -> None:
    variances, assumptions = build_timing_variances(
        run_id="run-1",
        payroll_expected=[_payroll_item("30.00", "10.00")],
        period_end=date(2025, 1, 31),
        as_of_date=date(2025, 2, 24),
        policy=UKTimingPolicy(),
    )
    assert {item.code for item in variances} == {"UKT-001"}
    assert all(item.severity == "review" for item in variances)
    assert assumptions["enabled"] is True


def test_overdue_after_visibility_window() -> None:
    variances, _ = build_timing_variances(
        run_id="run-2",
        payroll_expected=[_payroll_item("25.00", "0.00")],
        period_end=date(2025, 1, 31),
        as_of_date=date(2025, 2, 28),
        policy=UKTimingPolicy(),
    )
    assert [item.code for item in variances] == ["UKT-002"]
    assert variances[0].severity == "blocker"
    assert int(variances[0].details["days_overdue"]) >= 1


def test_business_day_rollover_uses_uk_holidays() -> None:
    due_date = compute_due_date(date(2025, 3, 31), 22)
    # 22 Apr 2025 + 3 UK business days should skip weekend.
    visibility = add_business_days(due_date, 3, "GB")
    assert visibility.isoformat() == "2025-04-25"
