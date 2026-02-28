from __future__ import annotations

from pathlib import Path

from libs.core.engine import reconcile_bank
from libs.core.harness.golden import load_scenario, normalize_payload, run_scenario


def test_same_input_is_byte_identical() -> None:
    scenario = Path(__file__).resolve().parent / "scenarios" / "tied_split"
    first = run_scenario(scenario, run_id="determinism-run")
    second = run_scenario(scenario, run_id="determinism-run")
    assert normalize_payload(first) == normalize_payload(second)


def test_input_order_shuffle_is_still_stable() -> None:
    scenario = Path(__file__).resolve().parent / "scenarios" / "tied_split"
    expected, bank, policy, accounts = load_scenario(scenario)

    baseline = reconcile_bank(
        run_id="determinism-shuffle",
        expected_items=expected,
        bank_items=bank,
        policy=policy,
        allowed_accounts=accounts,
    ).to_dict()

    shuffled = reconcile_bank(
        run_id="determinism-shuffle",
        expected_items=list(reversed(expected)),
        bank_items=list(reversed(bank)),
        policy=policy,
        allowed_accounts=accounts,
    ).to_dict()

    assert normalize_payload(baseline) == normalize_payload(shuffled)
