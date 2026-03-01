from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from .types import GLJournalLine, GLReconResult, GLTieOutSummary, PayrollExpected, Variance, q2


BUCKETS = ("net_pay_control", "taxes", "pension", "other")
BUCKET_CODES = {
    "net_pay_control": "GL-011",
    "taxes": "GL-012",
    "pension": "GL-013",
    "other": "GL-014",
}


def _variance_id(run_id: str, code: str, key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{run_id}:{code}:{key}"))


def reconcile_gl(
    run_id: str,
    payroll_expected: list[PayrollExpected],
    gl_lines: list[GLJournalLine],
    bucket_accounts: dict[str, set[str]],
    tolerance: Decimal = Decimal("0.01"),
) -> GLReconResult:
    tol = q2(tolerance)
    event_date = max((line.entry_date for line in gl_lines), default=None)

    payroll_totals = {
        "net_pay_control": q2(sum((item.net_amount for item in payroll_expected), start=Decimal("0"))),
        "taxes": q2(sum((item.tax_amount for item in payroll_expected), start=Decimal("0"))),
        "pension": q2(sum((item.pension_amount for item in payroll_expected), start=Decimal("0"))),
        "other": q2(sum((item.other_amount for item in payroll_expected), start=Decimal("0"))),
    }

    variances: list[Variance] = []

    if not gl_lines:
        variances.append(
            Variance(
                id=_variance_id(run_id, "GL-001", "missing_journal"),
                code="GL-001",
                category="gl",
                severity="blocker",
                status="open",
                message="No GL journal lines available for run",
                details={"rule": "journal_presence"},
            )
        )

    debit_total = q2(sum((line.debit_amount for line in gl_lines), start=Decimal("0")))
    credit_total = q2(sum((line.credit_amount for line in gl_lines), start=Decimal("0")))
    balance_delta = q2(credit_total - debit_total)
    is_balanced = q2(abs(balance_delta)) <= tol

    if gl_lines and not is_balanced:
        variances.append(
            Variance(
                id=_variance_id(run_id, "GL-002", "unbalanced"),
                code="GL-002",
                category="gl",
                severity="blocker",
                status="open",
                message="GL journal is unbalanced (debits != credits)",
                amount=q2(abs(balance_delta)),
                event_date=event_date,
                details={
                    "debit_total": f"{debit_total:.2f}",
                    "credit_total": f"{credit_total:.2f}",
                },
            )
        )

    gl_totals: dict[str, Decimal] = {}
    deltas: dict[str, Decimal] = {}
    for bucket in BUCKETS:
        mapped_accounts = bucket_accounts.get(bucket, set())
        gl_total = q2(
            abs(
                sum(
                    (line.net_amount for line in gl_lines if line.account_code in mapped_accounts),
                    start=Decimal("0"),
                )
            )
        )
        gl_totals[bucket] = gl_total
        delta = q2(payroll_totals[bucket] - gl_total)
        deltas[bucket] = delta
        if q2(abs(delta)) > tol:
            code = BUCKET_CODES[bucket]
            variances.append(
                Variance(
                    id=_variance_id(run_id, code, bucket),
                    code=code,
                    category="gl",
                    severity="blocker",
                    status="open",
                    message=f"GL bucket mismatch for {bucket}",
                    amount=q2(abs(delta)),
                    event_date=event_date,
                    details={
                        "bucket": bucket,
                        "payroll_total": f"{payroll_totals[bucket]:.2f}",
                        "gl_total": f"{gl_total:.2f}",
                        "mapped_accounts": sorted(mapped_accounts),
                    },
                )
            )

    variances.sort(key=lambda item: (item.code, item.id))
    has_blocker = any(item.severity == "blocker" and item.status == "open" for item in variances)
    status = "Not tied" if has_blocker else "Tied"

    summary = GLTieOutSummary(
        payroll_totals=payroll_totals,
        gl_totals=gl_totals,
        deltas=deltas,
        is_balanced=is_balanced,
        status=status,
        rules_used={
            "tolerance": f"{tol:.2f}",
            "buckets": {
                bucket: sorted(bucket_accounts.get(bucket, set()))
                for bucket in BUCKETS
            },
            "checks": [
                "journal_presence",
                "journal_balanced",
                "bucket_totals_compare",
            ],
        },
    )
    return GLReconResult(variances=variances, summary=summary)
