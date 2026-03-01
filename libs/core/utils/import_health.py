from __future__ import annotations

from typing import Any

REQUIRED_FILE_KINDS = {"bank", "payroll", "gl"}


def import_health_band(score: int) -> str:
    if score >= 85:
        return "green"
    if score >= 60:
        return "amber"
    return "red"


def compute_import_health_score(
    *,
    present_file_kinds: set[str],
    mapped_file_kinds: set[str],
    open_drift_blockers: int,
    failed_jobs: int,
    total_jobs: int,
) -> tuple[int, str, dict[str, Any]]:
    has_required_files = REQUIRED_FILE_KINDS.issubset(present_file_kinds)
    mapping_complete = REQUIRED_FILE_KINDS.issubset(mapped_file_kinds)
    no_drift = open_drift_blockers == 0

    ratio_factor = 1.0
    if total_jobs > 0:
        ratio_factor = max(0.0, 1.0 - (failed_jobs / total_jobs))
    jobs_points = int(round(10 * ratio_factor))

    score = (
        (40 if has_required_files else 0)
        + (30 if no_drift else 0)
        + (20 if mapping_complete else 0)
        + jobs_points
    )
    score = max(0, min(100, score))
    factors = {
        "has_required_files": has_required_files,
        "no_open_import_drift": no_drift,
        "mapping_complete_for_required_kinds": mapping_complete,
        "job_failure_ratio": 0 if total_jobs == 0 else failed_jobs / total_jobs,
        "required_file_kinds": sorted(REQUIRED_FILE_KINDS),
        "present_file_kinds": sorted(present_file_kinds),
        "mapped_file_kinds": sorted(mapped_file_kinds),
        "open_drift_blockers": open_drift_blockers,
        "failed_jobs": failed_jobs,
        "total_jobs": total_jobs,
        "score_breakdown": {
            "required_files": 40 if has_required_files else 0,
            "drift_blockers": 30 if no_drift else 0,
            "mapping_coverage": 20 if mapping_complete else 0,
            "jobs_ratio": jobs_points,
        },
    }
    return score, import_health_band(score), factors
