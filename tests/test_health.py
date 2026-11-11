import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from writer_service.app import create_app
from writer_service.config import Settings


def client() -> TestClient:
    return TestClient(create_app(Settings(_env_file=None)))  # type: ignore[call-arg]


def test_health_returns_ok() -> None:
    resp = client().get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_reports_version_header() -> None:
    resp = client().get("/health")
    assert resp.headers["x-service-version"] == "0.1.0"


def test_settings_attached_to_app_state() -> None:
    settings = Settings(_env_file=None, self_consistency_runs=1)  # type: ignore[call-arg]
    assert create_app(settings).state.settings is settings


def test_create_app_fails_fast_on_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WRITER_SELF_CONSISTENCY_RUNS", "0")
    with pytest.raises(ValidationError):
        create_app()
