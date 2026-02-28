from __future__ import annotations

from pathlib import Path

from libs.core.harness.golden import load_expected, normalize_payload, run_scenario


SCENARIO_ROOT = Path(__file__).resolve().parent / "scenarios"


def test_golden_scenarios_match_expected() -> None:
    for scenario in sorted(path for path in SCENARIO_ROOT.iterdir() if path.is_dir()):
        actual = run_scenario(scenario, run_id=f"golden-{scenario.name}")
        expected = load_expected(scenario)
        assert normalize_payload(actual["summary"]) == normalize_payload(expected["summary"])
        assert normalize_payload(actual["match_groups"]) == normalize_payload(expected["match_groups"])
        assert normalize_payload(actual["variances"]) == normalize_payload(expected["variances"])
