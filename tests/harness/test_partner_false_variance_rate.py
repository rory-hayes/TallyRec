from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from libs.core.engine import PayrollExpected, UKTimingPolicy, add_business_days, build_timing_variances, compute_due_date


def _baseline_false_variances(case: dict[str, str]) -> int:
    as_of_date = date.fromisoformat(case["as_of_date"])
    period_end = date.fromisoformat(case["period_end"])
    false_count = 0
    liabilities = [
        ("taxes", Decimal(case["tax_amount"]), 22),
        ("pension", Decimal(case["pension_amount"]), 22),
    ]
    for _liability_type, amount, due_day in liabilities:
        if amount <= Decimal("0.00"):
            continue
        due_date = compute_due_date(period_end, due_day)
        visibility_date = add_business_days(due_date, 3, "GB")
        baseline_marks_overdue = as_of_date > due_date
        if baseline_marks_overdue and as_of_date <= visibility_date:
            false_count += 1
    return false_count


def _sprint4_false_variances(case: dict[str, str]) -> int:
    as_of_date = date.fromisoformat(case["as_of_date"])
    period_end = date.fromisoformat(case["period_end"])
    payroll = [
        PayrollExpected(
            id=f"exp-{case['name']}",
            payment_date=period_end,
            net_amount=Decimal("0.00"),
            tax_amount=Decimal(case["tax_amount"]),
            pension_amount=Decimal(case["pension_amount"]),
            other_amount=Decimal("0.00"),
        )
    ]
    variances, _ = build_timing_variances(
        run_id=f"partner-{case['name']}",
        payroll_expected=payroll,
        period_end=period_end,
        as_of_date=as_of_date,
        policy=UKTimingPolicy(),
    )
    false_count = 0
    for variance in variances:
        if variance.code != "UKT-002":
            continue
        visibility_date = date.fromisoformat(variance.details["visibility_date"])
        if as_of_date <= visibility_date:
            false_count += 1
    return false_count


def test_partner_false_variance_rate_improves_vs_baseline() -> None:
    fixture_path = Path(__file__).resolve().parents[1] / "partner_fixtures" / "uk_timing_partner_cases.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    cases = payload["cases"]

    baseline_false = sum(_baseline_false_variances(case) for case in cases)
    sprint4_false = sum(_sprint4_false_variances(case) for case in cases)

    assert baseline_false > 0
    assert sprint4_false < baseline_false
