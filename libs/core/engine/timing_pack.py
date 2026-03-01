from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import holidays

from .types import PayrollExpected, Variance, q2


def _variance_id(run_id: str, code: str, key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{run_id}:{code}:{key}"))


@dataclass(frozen=True, slots=True)
class UKTimingPolicy:
    tax_due_day: int = 22
    pension_due_day: int = 22
    bacs_visibility_business_days: int = 3
    holiday_calendar: str = "GB"
    enabled: bool = True


def compute_liability_totals(payroll_expected: list[PayrollExpected]) -> dict[str, Decimal]:
    return {
        "taxes": q2(sum((item.tax_amount for item in payroll_expected), start=Decimal("0"))),
        "pension": q2(sum((item.pension_amount for item in payroll_expected), start=Decimal("0"))),
    }


def compute_due_date(period_end: date, due_day: int) -> date:
    next_month_year = period_end.year + (1 if period_end.month == 12 else 0)
    next_month = 1 if period_end.month == 12 else period_end.month + 1
    capped_day = min(max(1, due_day), monthrange(next_month_year, next_month)[1])
    return date(next_month_year, next_month, capped_day)


def add_business_days(base_date: date, count: int, holiday_calendar: str = "GB") -> date:
    if count <= 0:
        return base_date
    calendar = holidays.country_holidays(holiday_calendar)
    value = base_date
    added = 0
    while added < count:
        value = value + timedelta(days=1)
        if value.weekday() >= 5:
            continue
        if value in calendar:
            continue
        added += 1
    return value


def classify_liability(as_of_date: date, due_date: date, visibility_date: date, amount: Decimal) -> tuple[str, int]:
    if q2(amount) <= Decimal("0.00"):
        return "none", 0
    if as_of_date <= visibility_date:
        return "expected_later", 0
    return "overdue", (as_of_date - visibility_date).days


def build_timing_variances(
    *,
    run_id: str,
    payroll_expected: list[PayrollExpected],
    period_end: date,
    as_of_date: date,
    policy: UKTimingPolicy,
) -> tuple[list[Variance], dict[str, Any]]:
    if not policy.enabled:
        return [], {
            "enabled": False,
            "as_of_date": as_of_date.isoformat(),
            "policy": {
                "tax_due_day": policy.tax_due_day,
                "pension_due_day": policy.pension_due_day,
                "bacs_visibility_business_days": policy.bacs_visibility_business_days,
                "holiday_calendar": policy.holiday_calendar,
            },
        }

    totals = compute_liability_totals(payroll_expected)
    liabilities = [
        ("taxes", policy.tax_due_day),
        ("pension", policy.pension_due_day),
    ]
    variances: list[Variance] = []
    assumptions: dict[str, Any] = {
        "enabled": True,
        "as_of_date": as_of_date.isoformat(),
        "period_end": period_end.isoformat(),
        "policy": {
            "tax_due_day": policy.tax_due_day,
            "pension_due_day": policy.pension_due_day,
            "bacs_visibility_business_days": policy.bacs_visibility_business_days,
            "holiday_calendar": policy.holiday_calendar,
        },
        "liabilities": [],
    }

    for liability_type, due_day in liabilities:
        amount = q2(totals.get(liability_type, Decimal("0")))
        if amount <= Decimal("0.00"):
            continue
        due_date = compute_due_date(period_end, due_day)
        visibility_date = add_business_days(
            due_date,
            policy.bacs_visibility_business_days,
            holiday_calendar=policy.holiday_calendar,
        )
        classification, days_overdue = classify_liability(as_of_date, due_date, visibility_date, amount)

        assumptions["liabilities"].append(
            {
                "liability_type": liability_type,
                "amount": f"{amount:.2f}",
                "due_date": due_date.isoformat(),
                "visibility_date": visibility_date.isoformat(),
                "classification": classification,
            }
        )

        if classification == "none":
            continue

        base_details = {
            "liability_type": liability_type,
            "expected_amount": f"{amount:.2f}",
            "period_end": period_end.isoformat(),
            "due_date": due_date.isoformat(),
            "visibility_date": visibility_date.isoformat(),
            "as_of_date": as_of_date.isoformat(),
            "reason": (
                f"Liability not yet expected to be visible in bank data "
                f"until {visibility_date.isoformat()} (Bacs + business-day lag)"
            ),
        }

        if classification == "expected_later":
            variances.append(
                Variance(
                    id=_variance_id(run_id, "UKT-001", f"{liability_type}:{due_date.isoformat()}"),
                    code="UKT-001",
                    category="bank",
                    severity="review",
                    status="open",
                    message="Liability payment expected later under UK timing policy",
                    amount=amount,
                    event_date=due_date,
                    details=base_details,
                )
            )
        elif classification == "overdue":
            overdue_details = dict(base_details)
            overdue_details["days_overdue"] = days_overdue
            overdue_details["reason"] = (
                f"Liability payment overdue after visibility date {visibility_date.isoformat()}"
            )
            variances.append(
                Variance(
                    id=_variance_id(run_id, "UKT-002", f"{liability_type}:{due_date.isoformat()}"),
                    code="UKT-002",
                    category="bank",
                    severity="blocker",
                    status="open",
                    message="Liability payment overdue under UK timing policy",
                    amount=amount,
                    event_date=due_date,
                    details=overdue_details,
                )
            )

    variances.sort(
        key=lambda item: (
            item.code,
            str(item.details.get("liability_type", "")),
            str(item.details.get("due_date", "")),
            item.id,
        )
    )
    assumptions["liabilities"] = sorted(
        assumptions["liabilities"],
        key=lambda item: (item["liability_type"], item["due_date"]),
    )
    return variances, assumptions
