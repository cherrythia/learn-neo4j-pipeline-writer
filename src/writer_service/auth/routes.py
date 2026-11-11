"""Login / logout / who-am-I for the admin UI."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from writer_service.auth.deps import (
    SESSION_COOKIE,
    EngineDep,
    SettingsDep,
    UserDep,
    require_csrf_header,
)
from writer_service.auth.tokens import issue_token
from writer_service.auth.users import authenticate
from writer_service.governance.uow import UnitOfWork

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


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
    with UnitOfWork(engine) as uow:
        user = authenticate(uow, body.username, body.password)
        if user is None:
            uow.audit(body.username[:100], "auth.login_failed", "user", body.username[:64])
            failed = True
        else:
            uow.audit(user.username, "auth.login", "user", user.username)
            failed = False
            me = Me(username=user.username, role=user.role)
    if failed:  # audit row committed above, then refuse
        raise HTTPException(401, "invalid username or password")
    token = issue_token(
        me.username, me.role, request.app.state.jwt_secret, settings.session_ttl_minutes
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


@router.post("/logout", status_code=204, dependencies=[Depends(require_csrf_header)])
def logout(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=Me)
def me(user: UserDep) -> Me:
    return Me(username=user.username, role=user.role)
