from fastapi.testclient import TestClient

from writer_service.app import create_app


def test_health_returns_ok() -> None:
    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_reports_version_header() -> None:
    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.headers["x-service-version"] == "0.1.0"
