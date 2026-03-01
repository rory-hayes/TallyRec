from __future__ import annotations

from pathlib import Path

import pytest

import libs.core.utils.storage as storage_mod


class FakeHTTPResponse:
    def __init__(self, status: int, payload: bytes = b"") -> None:
        self.status = status
        self._payload = payload

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


def test_storage_client_interface_raises() -> None:
    client = storage_mod.StorageClient()
    with pytest.raises(NotImplementedError):
        client.upload_bytes("bucket", "path", b"x", "application/octet-stream")
    with pytest.raises(NotImplementedError):
        client.download_bytes("bucket", "path")


def test_supabase_storage_client_upload_download(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_urlopen(req):
        seen.append(req.full_url)
        if req.get_method() == "POST":
            return FakeHTTPResponse(201)
        return FakeHTTPResponse(200, b"payload")

    monkeypatch.setattr(storage_mod.request, "urlopen", fake_urlopen)

    client = storage_mod.SupabaseStorageClient("https://demo.supabase.co/", "service-key")
    assert client._build_object_url("audit-packs", "runs/audit pack.zip") == (
        "https://demo.supabase.co/storage/v1/object/audit-packs/runs/audit%20pack.zip"
    )

    upload_uri = client.upload_bytes("audit-packs", "runs/audit pack.zip", b"blob", "application/zip")
    assert upload_uri == "supabase://audit-packs/runs/audit pack.zip"
    assert seen[0].endswith("/audit-packs/runs/audit%20pack.zip")

    payload = client.download_bytes("audit-packs", "runs/audit pack.zip")
    assert payload == b"payload"
    assert seen[1].endswith("/audit-packs/runs/audit%20pack.zip")


def test_supabase_storage_client_http_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    client = storage_mod.SupabaseStorageClient("https://demo.supabase.co", "service-key")

    monkeypatch.setattr(storage_mod.request, "urlopen", lambda _req: FakeHTTPResponse(500))
    with pytest.raises(RuntimeError, match="upload failed"):
        client.upload_bytes("audit-packs", "runs/a.zip", b"x", "application/zip")

    monkeypatch.setattr(storage_mod.request, "urlopen", lambda _req: FakeHTTPResponse(404))
    with pytest.raises(RuntimeError, match="download failed"):
        client.download_bytes("audit-packs", "runs/a.zip")


def test_get_storage_client_chooser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path))
    local = storage_mod.get_storage_client()
    assert isinstance(local, storage_mod.LocalFilesystemStorageClient)

    monkeypatch.setenv("SUPABASE_URL", "https://demo.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    supabase = storage_mod.get_storage_client()
    assert isinstance(supabase, storage_mod.SupabaseStorageClient)
