from __future__ import annotations

from libs.core.utils import compute_import_health_score, headers_hash, import_health_band, normalize_headers


def test_import_validation_normalize_and_hash_are_stable() -> None:
    headers = [" Date ", "Amount", "", "ACCOUNT ", " amount "]
    normalized = normalize_headers(headers)
    assert normalized == ["date", "amount", "account", "amount"]

    first = headers_hash(headers)
    second = headers_hash(["date", "amount", "account", "amount"])
    assert first == second
    assert len(first) == 64


def test_import_health_score_and_bands() -> None:
    score_green, band_green, factors_green = compute_import_health_score(
        present_file_kinds={"bank", "payroll", "gl"},
        mapped_file_kinds={"bank", "payroll", "gl"},
        open_drift_blockers=0,
        failed_jobs=0,
        total_jobs=4,
    )
    assert score_green == 100
    assert band_green == "green"
    assert import_health_band(score_green) == "green"
    assert factors_green["job_failure_ratio"] == 0

    score_amber, band_amber, _ = compute_import_health_score(
        present_file_kinds={"bank", "payroll", "gl"},
        mapped_file_kinds={"bank"},
        open_drift_blockers=0,
        failed_jobs=1,
        total_jobs=2,
    )
    assert 60 <= score_amber <= 84
    assert band_amber == "amber"
    assert import_health_band(score_amber) == "amber"

    score_red, band_red, factors_red = compute_import_health_score(
        present_file_kinds={"bank"},
        mapped_file_kinds=set(),
        open_drift_blockers=1,
        failed_jobs=3,
        total_jobs=3,
    )
    assert score_red < 60
    assert band_red == "red"
    assert import_health_band(score_red) == "red"
    assert factors_red["score_breakdown"]["drift_blockers"] == 0
