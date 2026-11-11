"""FastAPI dependencies: API key for ingestion, session cookie + roles for the admin API.

CSRF: the session is a cookie (SameSite=Lax), and every state-changing request that relies on
it must also send ``X-Requested-With: writer-admin``. Browsers don't let other sites add custom
headers to cross-site requests without CORS approval, so forged form posts are rejected.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import Engine

from writer_service.auth.tokens import TokenError, decode_token
from writer_service.auth.users import get_user
from writer_service.config import Settings
from writer_service.governance.uow import UnitOfWork

SESSION_COOKIE = "writer_session"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "writer-admin"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ROLE_RANK = {"reviewer": 1, "admin": 2}  # an admin can do everything a reviewer can


def app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def app_engine(request: Request) -> Engine:
    engine: Engine = request.app.state.engine
    return engine


SettingsDep = Annotated[Settings, Depends(app_settings)]
EngineDep = Annotated[Engine, Depends(app_engine)]


def require_api_key(
    settings: SettingsDep,
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    if settings.ingest_api_key is None:
        raise HTTPException(503, "ingestion disabled: set WRITER_INGEST_API_KEY")
    expected = settings.ingest_api_key.get_secret_value().encode()
    # bytes, not str: compare_digest raises TypeError on non-ASCII strings
    if x_api_key is None or not secrets.compare_digest(x_api_key.encode(), expected):
        raise HTTPException(401, "missing or invalid X-API-Key")


def require_csrf_header(request: Request) -> None:
    if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(403, f"missing {CSRF_HEADER}: {CSRF_VALUE} header")


@dataclass(frozen=True)
class CurrentUser:
    username: str
    role: str


def current_user(request: Request, engine: EngineDep) -> CurrentUser:
    """The logged-in user. Any state-changing request authenticated by the cookie must also
    carry the CSRF header, whichever dependency it uses."""
    require_csrf_header(request)
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "not logged in")
    try:
        claims = decode_token(token, request.app.state.jwt_secret)
    except TokenError as exc:
        raise HTTPException(401, str(exc)) from exc
    with UnitOfWork(engine) as uow:  # re-check: deactivated users lose access immediately
        user = get_user(uow, claims.username)
        if user is None or not user.active:
            raise HTTPException(401, "account disabled")
        if claims.session_version != user.session_version:
            raise HTTPException(401, "session revoked")
        return CurrentUser(user.username, user.role)  # role from the DB, not the token


UserDep = Annotated[CurrentUser, Depends(current_user)]


def require_role(role: str) -> Callable[..., CurrentUser]:
    needed = ROLE_RANK[role]

    def dependency(user: UserDep) -> CurrentUser:
        if ROLE_RANK.get(user.role, 0) < needed:
            raise HTTPException(403, f"requires role {role}")
        return user

    return dependency
