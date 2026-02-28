from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

MONEY_PLACES = Decimal("0.01")


def q2(value: Decimal) -> Decimal:
    return value.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class PayrollExpected:
    id: str
    payment_date: date
    net_amount: Decimal
    employee_ref: str | None = None


@dataclass(frozen=True, slots=True)
class BankTransaction:
    id: str
    posted_date: date
    amount_signed: Decimal
    account_ref: str | None = None
    description: str | None = None

    @property
    def withdrawal_abs(self) -> Decimal:
        return q2(abs(self.amount_signed))


@dataclass(frozen=True, slots=True)
class ReconPolicy:
    amount_tolerance: Decimal = Decimal("0.01")
    date_window_days: int = 5
    max_group_size: int = 5
    enable_one_to_many: bool = True
    enable_many_to_one: bool = True
    require_allowed_account: bool = True


@dataclass(slots=True)
class MatchGroup:
    id: str
    group_kind: str
    match_confidence: str
    expected_ids: list[str]
    bank_ids: list[str]
    expected_total: Decimal
    bank_total: Decimal
    delta: Decimal
    matched_on: date | None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["expected_total"] = f"{self.expected_total:.2f}"
        payload["bank_total"] = f"{self.bank_total:.2f}"
        payload["delta"] = f"{self.delta:.2f}"
        payload["matched_on"] = self.matched_on.isoformat() if self.matched_on else None
        return payload


@dataclass(slots=True)
class Variance:
    id: str
    code: str
    category: str
    severity: str
    status: str
    message: str
    amount: Decimal | None = None
    event_date: date | None = None
    account_ref: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["amount"] = f"{self.amount:.2f}" if self.amount is not None else None
        payload["event_date"] = self.event_date.isoformat() if self.event_date else None
        return payload


@dataclass(slots=True)
class RunSummary:
    expected_net_pay: Decimal
    matched_bank_total: Decimal
    delta: Decimal
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_net_pay": f"{self.expected_net_pay:.2f}",
            "matched_bank_total": f"{self.matched_bank_total:.2f}",
            "delta": f"{self.delta:.2f}",
            "status": self.status,
        }


@dataclass(slots=True)
class ReconResult:
    match_groups: list[MatchGroup]
    variances: list[Variance]
    summary: RunSummary

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary.to_dict(),
            "match_groups": [group.to_dict() for group in self.match_groups],
            "variances": [variance.to_dict() for variance in self.variances],
        }
