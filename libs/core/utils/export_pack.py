from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from decimal import Decimal
from typing import Any
from uuid import UUID


def _escape_pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return str(value)


def build_minimal_pdf(lines: list[str]) -> bytes:
    y = 800
    rendered = ["BT", "/F1 11 Tf"]
    for line in lines:
        rendered.append(f"72 {y} Td ({_escape_pdf_text(line)}) Tj")
        y -= 14
    rendered.append("ET")
    stream_data = "\n".join(rendered).encode("utf-8")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream_data)} >>\nstream\n".encode("utf-8") + stream_data + b"\nendstream",
    ]

    body = io.BytesIO()
    body.write(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(body.tell())
        body.write(f"{index} 0 obj\n".encode("utf-8"))
        body.write(obj)
        body.write(b"\nendobj\n")

    xref_offset = body.tell()
    body.write(f"xref\n0 {len(objects) + 1}\n".encode("utf-8"))
    body.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.write(f"{offset:010d} 00000 n \n".encode("utf-8"))
    body.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode(
            "utf-8"
        )
    )
    return body.getvalue()


def csv_bytes(rows: list[dict[str, Any]], fieldnames: list[str]) -> bytes:
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key) for key in fieldnames})
    return handle.getvalue().encode("utf-8")


def deterministic_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(files.keys()):
            info = zipfile.ZipInfo(path)
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, files[path])
    return buffer.getvalue()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_export_pack(
    *,
    run_id: str,
    summary: dict[str, Any],
    gl_summary: dict[str, Any] | None,
    variances: list[dict[str, Any]],
    audit_events: list[dict[str, Any]],
    source_files: list[dict[str, Any]],
    signoff: dict[str, Any],
) -> tuple[bytes, str, dict[str, Any]]:
    rules_used = {
        "bank_reconciliation": "bank_v1_deterministic",
        "gl_reconciliation": "gl_v1_deterministic",
        "variance_resolution": ["matched", "explained", "expected_later", "ignored_reviewer_required"],
    }

    generated_at = (
        summary.get("tieout", {}).get("computed_at")
        or summary.get("gl_tieout", {}).get("computed_at")
        or signoff.get("reviewed_at")
        or "1970-01-01T00:00:00+00:00"
    )
    timing = summary.get("tieout", {}).get("policy_snapshot", {}).get("timing", {})
    import_health = summary.get("import_health") or {}
    drift_events = summary.get("drift_events") or {}

    pdf_lines = [
        f"Tally Audit Pack - Run {run_id}",
        f"Generated: {generated_at}",
        f"Run status: {summary.get('run_status')}",
        f"Payday date: {summary.get('payday_date')}",
        f"Must close by: {summary.get('must_close_by_date')}",
        f"SLA reminder state: {summary.get('sla_reminder_state')}",
        f"Bank tie status: {summary.get('tieout', {}).get('status')}",
        f"GL tie status: {gl_summary.get('status') if gl_summary else 'not_computed'}",
        f"Open blockers: {summary.get('open_variances', {}).get('blocker', 0)}",
        f"Open review: {summary.get('open_variances', {}).get('review', 0)}",
        f"Import health: {import_health.get('health_band')} ({import_health.get('health_score')})",
        f"Drift events: {drift_events.get('count', 0)}",
        "Timing assumptions:",
        json.dumps(_json_safe(timing), sort_keys=True),
        "Rules:",
        json.dumps(rules_used, sort_keys=True),
        "Sign-off:",
        json.dumps(_json_safe(signoff), sort_keys=True),
    ]

    variance_rows = [
        {
            "id": row.get("id"),
            "code": row.get("code"),
            "severity": row.get("severity"),
            "status": row.get("status"),
            "message": row.get("message"),
            "amount": row.get("amount"),
            "event_date": row.get("event_date"),
            "note": row.get("note"),
            "changed_by": row.get("changed_by"),
            "changed_at": row.get("changed_at"),
        }
        for row in sorted(variances, key=lambda item: (item.get("code") or "", item.get("id") or ""))
    ]

    audit_rows = [
        {
            "id": row.get("id"),
            "event_type": row.get("event_type"),
            "actor_user_id": row.get("actor_user_id"),
            "created_at": row.get("created_at"),
            "entity_type": row.get("entity_type"),
            "entity_id": row.get("entity_id"),
        }
        for row in sorted(audit_events, key=lambda item: (item.get("created_at") or "", item.get("id") or ""))
    ]

    file_manifest = [
        {
            "file_id": row.get("id"),
            "file_kind": row.get("file_kind"),
            "checksum_sha256": row.get("checksum_sha256"),
            "created_at": row.get("created_at"),
            "uri": row.get("uri"),
        }
        for row in sorted(source_files, key=lambda item: (item.get("file_kind") or "", item.get("id") or ""))
    ]

    manifest = {
        "run_id": run_id,
        "summary": _json_safe(summary),
        "gl_summary": _json_safe(gl_summary),
        "rules_used": rules_used,
        "signoff": _json_safe(signoff),
        "timing": _json_safe(timing),
        "import_health": _json_safe(import_health),
        "drift_events": _json_safe(drift_events),
        "source_files": file_manifest,
        "variance_count": len(variance_rows),
        "audit_event_count": len(audit_rows),
    }

    files = {
        "summary.pdf": build_minimal_pdf(pdf_lines),
        "variances.csv": csv_bytes(
            variance_rows,
            ["id", "code", "severity", "status", "message", "amount", "event_date", "note", "changed_by", "changed_at"],
        ),
        "audit_excerpt.csv": csv_bytes(
            audit_rows,
            ["id", "event_type", "actor_user_id", "created_at", "entity_type", "entity_id"],
        ),
        "manifest.json": (json.dumps(_json_safe(manifest), sort_keys=True, indent=2) + "\n").encode("utf-8"),
    }

    blob = deterministic_zip(files)
    pack_hash = sha256_hex(blob)
    return blob, pack_hash, manifest
