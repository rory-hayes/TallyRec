from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.app.main import app


def test_preflight_allows_vercel_origin_for_resolution_endpoint() -> None:
    origin = "https://web-ebon-eight-63.vercel.app"
    headers = {
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-user-id",
    }
    with TestClient(app) as client:
        response = client.options("/v1/variances/00000000-0000-0000-0000-000000000001/resolve", headers=headers)

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == origin
    assert "x-user-id" in response.headers.get("access-control-allow-headers", "").lower()


def test_health_includes_cors_headers_for_local_dev_origin() -> None:
    origin = "http://localhost:3000"
    with TestClient(app) as client:
        response = client.get("/v1/health", headers={"Origin": origin})

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == origin


def test_preflight_rejects_unknown_origin() -> None:
    origin = "https://evil.example.com"
    headers = {
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-user-id",
    }
    with TestClient(app) as client:
        response = client.options("/v1/variances/00000000-0000-0000-0000-000000000001/resolve", headers=headers)

    assert response.status_code == 400
    assert response.headers.get("access-control-allow-origin") is None
