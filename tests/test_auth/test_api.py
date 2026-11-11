from fastapi.testclient import TestClient
from sqlalchemy import select

from writer_service.auth.deps import SESSION_COOKIE
from writer_service.config import Settings
from writer_service.governance.db import create_engine_for
from writer_service.governance.models import AuditLog, User
from writer_service.governance.uow import UnitOfWork

from .conftest import API_KEY, CSRF, login, protected_app

# ------------------------------------------------------------------- API key


def test_ingest_requires_api_key(client: TestClient) -> None:
    assert client.post("/test/ingest").status_code == 401
    assert client.post("/test/ingest", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.post("/test/ingest", headers={"X-API-Key": API_KEY}).status_code == 200


def test_ingest_refused_when_no_key_configured(settings: Settings) -> None:
    app = protected_app(settings.model_copy(update={"ingest_api_key": None}))
    with TestClient(app) as c:
        r = c.post("/test/ingest", headers={"X-API-Key": "anything"})
    assert r.status_code == 503
    assert "WRITER_INGEST_API_KEY" in r.json()["detail"]


# ------------------------------------------------------------------- login


def test_login_sets_httponly_cookie_and_me_works(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "alice-password-123"},
        headers=CSRF,
    )
    assert resp.status_code == 200
    assert resp.json() == {"username": "alice", "role": "admin"}
    cookie = resp.headers["set-cookie"]
    assert (
        f"{SESSION_COOKIE}=" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    )
    assert client.get("/api/auth/me").json() == {"username": "alice", "role": "admin"}


def test_wrong_password_and_unknown_user_look_the_same(client: TestClient) -> None:
    a = client.post(
        "/api/auth/login", json={"username": "alice", "password": "nope-nope-nope"}, headers=CSRF
    )
    b = client.post(
        "/api/auth/login", json={"username": "ghost", "password": "nope-nope-nope"}, headers=CSRF
    )
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json() == {"detail": "invalid username or password"}


def test_logins_are_audited(client: TestClient, settings: Settings) -> None:
    login(client, "alice", "alice-password-123")
    client.post(
        "/api/auth/login", json={"username": "alice", "password": "bad-bad-bad-bad"}, headers=CSRF
    )
    engine = create_engine_for(settings.governance_db_url)
    with UnitOfWork(engine) as uow:
        actions = [
            (a.actor, a.action)
            for a in uow.session.scalars(select(AuditLog))
            if a.action.startswith("auth.")
        ]
    assert ("alice", "auth.login") in actions and ("alice", "auth.login_failed") in actions


def test_me_requires_session(client: TestClient) -> None:
    assert client.get("/api/auth/me").status_code == 401
    client.cookies.set(SESSION_COOKIE, "garbage")
    assert client.get("/api/auth/me").status_code == 401


def test_logout_clears_cookie(client: TestClient) -> None:
    login(client, "alice", "alice-password-123")
    assert client.post("/api/auth/logout", headers=CSRF).status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_deactivated_user_loses_access(client: TestClient, settings: Settings) -> None:
    login(client, "alice", "alice-password-123")
    engine = create_engine_for(settings.governance_db_url)
    with UnitOfWork(engine) as uow:
        uow.session.scalars(select(User).where(User.username == "alice")).one().active = False
    assert client.get("/api/auth/me").status_code == 401


# ------------------------------------------------------------------- roles + CSRF


def test_roles(client: TestClient) -> None:
    login(client, "rita", "rita-password-123")
    assert client.post("/test/review", headers=CSRF).json() == {"user": "rita"}
    assert client.post("/test/publish", headers=CSRF).status_code == 403
    login(client, "alice", "alice-password-123")
    assert client.post("/test/publish", headers=CSRF).status_code == 200
    assert client.post("/test/review", headers=CSRF).status_code == 200  # admin can review


def test_cookie_writes_require_csrf_header(client: TestClient) -> None:
    login(client, "alice", "alice-password-123")
    r = client.post("/test/publish")
    assert r.status_code == 403 and "X-Requested-With" in r.json()["detail"]


def test_role_routes_require_login(client: TestClient) -> None:
    assert client.post("/test/review", headers=CSRF).status_code == 401
