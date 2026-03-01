from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from libs.core.engine import GLJournalLine, PayrollExpected, reconcile_gl


def test_gl_golden_scenarios() -> None:
    root = Path(__file__).resolve().parent / "gl_scenarios"
    for scenario in sorted(path for path in root.iterdir() if path.is_dir()):
        payload = json.loads((scenario / "inputs" / "payload.json").read_text(encoding="utf-8"))
        expected = json.loads((scenario / "expected" / "result.json").read_text(encoding="utf-8"))

        payroll = [
            PayrollExpected(
                id=item["id"],
                payment_date=date.fromisoformat(item["payment_date"]),
                net_amount=Decimal(item["net_amount"]),
                tax_amount=Decimal(item["tax_amount"]),
                pension_amount=Decimal(item["pension_amount"]),
                other_amount=Decimal(item["other_amount"]),
            )
            for item in payload["payroll_expected"]
        ]
        gl_lines = [
            GLJournalLine(
                id=item["id"],
                entry_date=date.fromisoformat(item["entry_date"]),
                account_code=item["account_code"],
                debit_amount=Decimal(item["debit_amount"]),
                credit_amount=Decimal(item["credit_amount"]),
            )
            for item in payload["gl_lines"]
        ]
        bucket_accounts = {key: set(value) for key, value in payload["bucket_accounts"].items()}

        result = reconcile_gl(
            run_id=f"golden-{scenario.name}",
            payroll_expected=payroll,
            gl_lines=gl_lines,
            bucket_accounts=bucket_accounts,
            tolerance=Decimal(payload["tolerance"]),
        )

        codes = sorted(item.code for item in result.variances)
        assert result.summary.status == expected["status"], scenario.name
        assert codes == sorted(expected["codes"]), scenario.name
