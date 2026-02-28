from __future__ import annotations

from pathlib import Path

from libs.core.harness.golden import load_scenario


def test_load_scenario_uses_default_policy_when_missing_policy_file(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario"
    inputs = scenario / "inputs"
    inputs.mkdir(parents=True)

    (inputs / "payroll_expected.csv").write_text(
        "id,payment_date,net_amount,employee_ref\nexp-1,2025-01-31,10.00,E1\n",
        encoding="utf-8",
    )
    (inputs / "bank_transactions.csv").write_text(
        "id,posted_date,amount_signed,account_ref,description\nbnk-1,2025-01-31,-10.00,ACCT-1,desc\n",
        encoding="utf-8",
    )

    _, _, policy, allowed = load_scenario(scenario)
    assert policy.date_window_days == 5
    assert allowed == set()
