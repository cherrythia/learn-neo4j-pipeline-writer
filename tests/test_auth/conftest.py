from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from writer_service.app import create_app
from writer_service.auth.deps import CurrentUser, require_api_key, require_role
from writer_service.auth.users import create_user
from writer_service.config import Settings
from writer_service.governance.db import create_engine_for
from writer_service.governance.migrate import upgrade
from writer_service.governance.uow import UnitOfWork

API_KEY = "test-ingest-key-0123456789"
CSRF = {"X-Requested-With": "writer-admin"}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    upgrade(url)
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        governance_db_url=url,
        ingest_api_key=API_KEY,
        admin_jwt_secret="x" * 40,
    )


def protected_app(settings: Settings) -> FastAPI:
    """The real app plus three throwaway routes that exercise each dependency."""
    app = create_app(settings)

    @app.post("/test/ingest", dependencies=[Depends(require_api_key)])
    def ingest() -> dict[str, str]:
        return {"ok": "ingest"}

    reviewer = Annotated[CurrentUser, Depends(require_role("reviewer"))]
    admin = Annotated[CurrentUser, Depends(require_role("admin"))]

    @app.post("/test/review")
    def review(user: reviewer) -> dict[str, str]:
        return {"user": user.username}

    @app.post("/test/publish")
    def publish(user: admin) -> dict[str, str]:
        return {"user": user.username}

    return app


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    engine = create_engine_for(settings.governance_db_url)
    with UnitOfWork(engine) as uow:
        create_user(uow, "alice", "alice-password-123", "admin", actor="setup")
        create_user(uow, "rita", "rita-password-123", "reviewer", actor="setup")
    engine.dispose()
    with TestClient(protected_app(settings)) as c:
        yield c


def login(client: TestClient, username: str, password: str) -> None:
    resp = client.post(
        "/api/auth/login", json={"username": username, "password": password}, headers=CSRF
    )
    assert resp.status_code == 200, resp.text
