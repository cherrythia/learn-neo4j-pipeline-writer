"""Login / logout / who-am-I for the admin UI."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from writer_service.auth.deps import (
    SESSION_COOKIE,
    EngineDep,
    SettingsDep,
    UserDep,
    require_csrf_header,
)
from writer_service.auth.ratelimit import LoginLimiter
from writer_service.auth.tokens import issue_token
from writer_service.auth.users import authenticate, get_user
from writer_service.governance.uow import UnitOfWork

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


class Me(BaseModel):
    username: str
    role: str


@router.post("/login", response_model=Me, dependencies=[Depends(require_csrf_header)])
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    settings: SettingsDep,
    engine: EngineDep,
) -> Me:
    limiter: LoginLimiter = request.app.state.login_limiter
    client = request.client.host if request.client else "unknown"
    pair_key, client_key = limiter.keys(body.username, client)
    # Reserve the attempt before the slow password check, so parallel guesses can't overrun.
    if not limiter.attempt(pair_key, client_key):
        raise HTTPException(429, "too many failed logins; try again later")
    with UnitOfWork(engine) as uow:
        user = authenticate(uow, body.username, body.password)
        if user is None:
            # Never put attacker-supplied text in the actor column.
            uow.audit(
                "anonymous",
                "auth.login_failed",
                "user",
                body.username[:64],
                {"username": body.username[:200], "client": client},
            )
            me = None
        else:
            uow.audit(user.username, "auth.login", "user", user.username, {"client": client})
            me = Me(username=user.username, role=user.role)
            session_version = user.session_version
    if me is None:  # the audit row is committed before refusing; the attempt stays counted
        raise HTTPException(401, "invalid username or password")
    limiter.release(pair_key)  # this account's failures are forgiven
    limiter.refund(client_key)  # and a success doesn't count against the client
    token = issue_token(
        me.username,
        me.role,
        request.app.state.jwt_secret,
        settings.session_ttl_minutes,
        session_version,
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=settings.session_ttl_minutes * 60,
        path="/",
    )
    return me


@router.post("/logout", status_code=204)
def logout(response: Response, user: UserDep, engine: EngineDep) -> None:
    """Ends *every* session of this user (bumps the session version), then clears the cookie."""
    with UnitOfWork(engine) as uow:
        row = get_user(uow, user.username)
        if row is not None:
            row.session_version += 1
            uow.audit(user.username, "auth.logout", "user", user.username)
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=Me)
def me(user: UserDep) -> Me:
    return Me(username=user.username, role=user.role)
