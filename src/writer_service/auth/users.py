"""User management and credential checks."""

from __future__ import annotations

from typing import Literal

from sqlalchemy import select

from writer_service.auth.passwords import DUMMY_HASH, hash_password, verify_password
from writer_service.governance.models import User
from writer_service.governance.uow import UnitOfWork

Role = Literal["admin", "reviewer"]


def get_user(uow: UnitOfWork, username: str) -> User | None:
    return uow.session.scalars(select(User).where(User.username == username)).one_or_none()


def create_user(uow: UnitOfWork, username: str, password: str, role: Role, *, actor: str) -> User:
    if get_user(uow, username) is not None:
        raise ValueError(f"user {username!r} already exists")
    user = User(username=username, password_hash=hash_password(password), role=role)
    uow.session.add(user)
    uow.audit(actor, "user.create", "user", username, {"role": role})
    return user


def authenticate(uow: UnitOfWork, username: str, password: str) -> User | None:
    """The active user with these credentials, else None (same cost either way)."""
    user = get_user(uow, username)
    if user is None:
        verify_password(password, DUMMY_HASH)
        return None
    if not verify_password(password, user.password_hash) or not user.active:
        return None
    return user
