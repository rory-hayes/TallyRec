from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from libs.core.engine import BankTransaction, PayrollExpected, ReconPolicy, reconcile_bank


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
    result = reconcile_bank(
        run_id=run_id,
        expected_items=payroll_expected,
        bank_items=bank_transactions,
        policy=policy,
        allowed_accounts=allowed_accounts,
    )
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
