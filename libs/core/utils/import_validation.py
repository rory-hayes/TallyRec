from __future__ import annotations

import hashlib


def normalize_headers(headers: list[str]) -> list[str]:
    return [str(value).strip().lower() for value in headers if str(value).strip()]


def headers_hash(headers: list[str]) -> str:
    normalized = normalize_headers(headers)
    payload = "|".join(normalized).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
