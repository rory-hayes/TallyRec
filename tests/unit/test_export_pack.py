from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from libs.core.utils.export_pack import build_export_pack, deterministic_zip, sha256_hex
from libs.core.utils.storage import LocalFilesystemStorageClient


def test_export_pack_reproducible_hash() -> None:
    payload = {
        "run_id": "run-1",
        "summary": {"run_status": "approved", "tieout": {"status": "Tied", "computed_at": "2025-01-31T00:00:00+00:00"}, "open_variances": {"blocker": 0, "review": 0}},
        "gl_summary": {"status": "Tied", "computed_at": "2025-01-31T00:00:00+00:00"},
        "variances": [],
        "audit_events": [{"id": uuid4(), "event_type": "run.approved", "actor_user_id": "u1", "created_at": "2025-01-31T00:00:00+00:00", "entity_type": "run", "entity_id": uuid4()}],
        "source_files": [{"id": uuid4(), "file_kind": "bank", "checksum_sha256": "a" * 64, "created_at": "2025-01-31T00:00:00+00:00", "uri": "s3://x"}],
        "signoff": {"status": "approved", "prepared_by": "u2", "reviewer_id": "u3", "reviewed_at": "2025-01-31T00:00:00+00:00"},
    }

    blob1, hash1, manifest1 = build_export_pack(**payload)
    blob2, hash2, manifest2 = build_export_pack(**payload)
    assert hash1 == hash2
    assert blob1 == blob2
    assert manifest1 == manifest2


def test_local_storage_upload_and_download(tmp_path: Path) -> None:
    client = LocalFilesystemStorageClient(root=tmp_path)
    uri = client.upload_bytes("audit-packs", "runs/r1/pack.zip", b"payload", "application/zip")
    assert uri == "local://audit-packs/runs/r1/pack.zip"
    assert client.download_bytes("audit-packs", "runs/r1/pack.zip") == b"payload"


def test_deterministic_zip_and_hash() -> None:
    zip_a = deterministic_zip({"b.txt": b"2", "a.txt": b"1"})
    zip_b = deterministic_zip({"a.txt": b"1", "b.txt": b"2"})
    assert zip_a == zip_b
    assert sha256_hex(zip_a) == sha256_hex(zip_b)
