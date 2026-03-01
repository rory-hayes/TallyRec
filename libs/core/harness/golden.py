from __future__ import annotations

import csv
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from libs.core.engine import BankTransaction, PayrollExpected, ReconPolicy, UKTimingPolicy, Variance, build_timing_variances, reconcile_bank


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_scenario(scenario_dir: Path) -> tuple[list[PayrollExpected], list[BankTransaction], ReconPolicy, set[str]]:
    input_dir = scenario_dir / "inputs"
    payroll_rows = _read_csv(input_dir / "payroll_expected.csv")
    bank_rows = _read_csv(input_dir / "bank_transactions.csv")

    payroll_expected = [
        PayrollExpected(
            id=row["id"],
            employee_ref=row.get("employee_ref") or None,
            payment_date=date.fromisoformat(row["payment_date"]),
            net_amount=Decimal(row["net_amount"]),
            tax_amount=Decimal(row.get("tax_amount") or "0.00"),
            pension_amount=Decimal(row.get("pension_amount") or "0.00"),
            other_amount=Decimal(row.get("other_amount") or "0.00"),
        )
        for row in payroll_rows
    ]

    bank_transactions = [
        BankTransaction(
            id=row["id"],
            posted_date=date.fromisoformat(row["posted_date"]),
            amount_signed=Decimal(row["amount_signed"]),
            account_ref=row.get("account_ref") or None,
            description=row.get("description") or None,
        )
        for row in bank_rows
    ]

    policy_path = input_dir / "policy.json"
    if policy_path.exists():
        raw_policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy = ReconPolicy(
            amount_tolerance=Decimal(str(raw_policy.get("amount_tolerance", "0.01"))),
            date_window_days=int(raw_policy.get("date_window_days", 5)),
            max_group_size=int(raw_policy.get("max_group_size", 5)),
            enable_one_to_many=bool(raw_policy.get("enable_one_to_many", True)),
            enable_many_to_one=bool(raw_policy.get("enable_many_to_one", True)),
            require_allowed_account=bool(raw_policy.get("require_allowed_account", True)),
        )
    else:
        policy = ReconPolicy()

    allowed_accounts_path = input_dir / "allowed_accounts.csv"
    allowed_accounts: set[str] = set()
    if allowed_accounts_path.exists():
        rows = _read_csv(allowed_accounts_path)
        allowed_accounts = {row["account_ref"] for row in rows if row.get("account_ref")}

    return payroll_expected, bank_transactions, policy, allowed_accounts


def run_scenario(scenario_dir: Path, run_id: str = "golden-run") -> dict:
    payroll_expected, bank_transactions, policy, allowed_accounts = load_scenario(scenario_dir)
    context_path = scenario_dir / "inputs" / "context.json"
    context = json.loads(context_path.read_text(encoding="utf-8")) if context_path.exists() else {}

    result = reconcile_bank(
        run_id=run_id,
        expected_items=payroll_expected,
        bank_items=bank_transactions,
        policy=policy,
        allowed_accounts=allowed_accounts,
    )
    if "timing" in context:
        timing = context["timing"]
        timing_variances, _ = build_timing_variances(
            run_id=run_id,
            payroll_expected=payroll_expected,
            period_end=date.fromisoformat(timing["period_end"]),
            as_of_date=date.fromisoformat(timing["as_of_date"]),
            policy=UKTimingPolicy(
                tax_due_day=int(timing.get("tax_due_day", 22)),
                pension_due_day=int(timing.get("pension_due_day", 22)),
                bacs_visibility_business_days=int(timing.get("bacs_visibility_business_days", 3)),
                holiday_calendar=timing.get("holiday_calendar", "GB"),
                enabled=bool(timing.get("enabled", True)),
            ),
        )
        result.variances.extend(timing_variances)

    if "import_drift" in context:
        drift = context["import_drift"]
        result.variances.append(
            Variance(
                id=str(uuid5(NAMESPACE_URL, f"{run_id}:IMP-001:{drift.get('source_file_id', 'source-file')}")),
                code="IMP-001",
                category="import",
                severity="blocker",
                status="open",
                message="Source file headers drifted from expected mapping template",
                details={
                    "source_file_id": drift.get("source_file_id"),
                    "expected_headers": drift.get("expected_headers", []),
                    "observed_headers": drift.get("observed_headers", []),
                    "expected_header_hash": drift.get("expected_header_hash"),
                    "observed_header_hash": drift.get("observed_header_hash"),
                },
            )
        )

    result.variances.sort(key=lambda variance: (variance.code, variance.event_date or date.min, variance.id))
    has_blocker = any(item.severity == "blocker" and item.status == "open" for item in result.variances)
    has_review = any(item.severity == "review" and item.status == "open" for item in result.variances)
    if has_blocker:
        result.summary.status = "Not tied"
    elif has_review:
        result.summary.status = "Needs review"
    elif abs(result.summary.delta) <= policy.amount_tolerance:
        result.summary.status = "Tied"
    else:
        result.summary.status = "Not tied"
    return result.to_dict()


def load_expected(scenario_dir: Path) -> dict:
    expected_dir = scenario_dir / "expected"
    summary = json.loads((expected_dir / "summary.json").read_text(encoding="utf-8"))
    match_groups = json.loads((expected_dir / "match_groups.json").read_text(encoding="utf-8"))
    variances = json.loads((expected_dir / "variances.json").read_text(encoding="utf-8"))
    return {
        "summary": summary,
        "match_groups": match_groups,
        "variances": variances,
    }


def normalize_payload(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, indent=2)
