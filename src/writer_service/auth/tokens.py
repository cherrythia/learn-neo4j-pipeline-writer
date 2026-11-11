"""Short-lived session tokens (JWT, HS256) carried in an httpOnly cookie."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt


class TokenError(Exception):
    pass


@dataclass(frozen=True)
class Claims:
    username: str
    role: str  # informational; authorisation always uses the role stored in the database
    session_version: int = 0


def issue_token(
    username: str, role: str, secret: str, ttl_minutes: int, session_version: int = 0
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": username,
        "role": role,
        "sv": session_version,
        "iat": now,
        "exp": now + timedelta(minutes=ttl_minutes),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_token(token: str, secret: str) -> Claims:
    try:
        data = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "sub"]})
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("session expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("invalid session") from exc
    return Claims(
        username=str(data["sub"]),
        role=str(data.get("role", "")),
        session_version=int(data.get("sv", 0)),
    )
