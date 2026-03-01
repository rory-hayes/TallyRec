from __future__ import annotations

import os
from pathlib import Path
from urllib import parse, request


class StorageClient:
    def upload_bytes(self, bucket: str, object_path: str, data: bytes, content_type: str) -> str:
        raise NotImplementedError

    def download_bytes(self, bucket: str, object_path: str) -> bytes:
        raise NotImplementedError


class LocalFilesystemStorageClient(StorageClient):
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(os.getenv("LOCAL_STORAGE_ROOT", ".storage"))

    def upload_bytes(self, bucket: str, object_path: str, data: bytes, content_type: str) -> str:
        target = self.root / bucket / object_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return f"local://{bucket}/{object_path}"

    def download_bytes(self, bucket: str, object_path: str) -> bytes:
        target = self.root / bucket / object_path
        return target.read_bytes()


class SupabaseStorageClient(StorageClient):
    def __init__(self, supabase_url: str, service_role_key: str) -> None:
        self.supabase_url = supabase_url.rstrip("/")
        self.service_role_key = service_role_key

    def _build_object_url(self, bucket: str, object_path: str) -> str:
        escaped = parse.quote(object_path, safe="/")
        return f"{self.supabase_url}/storage/v1/object/{bucket}/{escaped}"

    def upload_bytes(self, bucket: str, object_path: str, data: bytes, content_type: str) -> str:
        req = request.Request(
            self._build_object_url(bucket, object_path),
            data=data,
            method="POST",
            headers={
                "apikey": self.service_role_key,
                "Authorization": f"Bearer {self.service_role_key}",
                "Content-Type": content_type,
                "x-upsert": "true",
            },
        )
        with request.urlopen(req) as response:
            if response.status not in (200, 201):
                raise RuntimeError(f"Supabase storage upload failed with status {response.status}")
        return f"supabase://{bucket}/{object_path}"

    def download_bytes(self, bucket: str, object_path: str) -> bytes:
        req = request.Request(
            self._build_object_url(bucket, object_path),
            method="GET",
            headers={
                "apikey": self.service_role_key,
                "Authorization": f"Bearer {self.service_role_key}",
            },
        )
        with request.urlopen(req) as response:
            if response.status != 200:
                raise RuntimeError(f"Supabase storage download failed with status {response.status}")
            return response.read()


def get_storage_client() -> StorageClient:
    supabase_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if supabase_url and service_role_key:
        return SupabaseStorageClient(supabase_url, service_role_key)
    return LocalFilesystemStorageClient()
