from __future__ import annotations

import argparse
import sys
from pathlib import Path

from libs.core.harness.golden import load_expected, normalize_payload, run_scenario


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Tally golden scenario regression harness")
    parser.add_argument(
        "--scenarios-dir",
        default="tests/harness/scenarios",
        help="Directory containing scenario subdirectories",
    )
    args = parser.parse_args()

    scenarios_dir = Path(args.scenarios_dir)
    failures: list[str] = []

    for scenario in sorted(path for path in scenarios_dir.iterdir() if path.is_dir()):
        actual = run_scenario(scenario, run_id=f"golden-{scenario.name}")
        expected = load_expected(scenario)
        if normalize_payload(actual["summary"]) != normalize_payload(expected["summary"]):
            failures.append(f"{scenario.name}: summary mismatch")
        if normalize_payload(actual["match_groups"]) != normalize_payload(expected["match_groups"]):
            failures.append(f"{scenario.name}: match_groups mismatch")
        if normalize_payload(actual["variances"]) != normalize_payload(expected["variances"]):
            failures.append(f"{scenario.name}: variances mismatch")

    if failures:
        print("Golden harness failures:")
        for failure in failures:
            print(f" - {failure}")
        return 1

    print(f"Golden harness passed for {len(list(path for path in scenarios_dir.iterdir() if path.is_dir()))} scenarios")
    return 0


if __name__ == "__main__":
    sys.exit(main())
