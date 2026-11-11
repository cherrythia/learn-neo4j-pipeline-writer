import time

import pytest

from writer_service.auth.passwords import hash_password, verify_password
from writer_service.auth.tokens import TokenError, decode_token, issue_token

SECRET = "s" * 40


def test_hash_roundtrip_and_salting() -> None:
    h1, h2 = hash_password("correct horse"), hash_password("correct horse")
    assert h1 != h2  # random salt
    assert h1.startswith("scrypt$")
    assert verify_password("correct horse", h1)
    assert not verify_password("wrong horse", h1)


@pytest.mark.parametrize("stored", ["", "plain", "scrypt$1$2", "bcrypt$x$y$z$a$b"])
def test_malformed_hash_never_verifies(stored: str) -> None:
    assert not verify_password("anything", stored)


def test_short_password_rejected() -> None:
    with pytest.raises(ValueError, match="at least 12"):
        hash_password("short")


def test_token_roundtrip() -> None:
    token = issue_token("alice", "admin", SECRET, ttl_minutes=5)
    claims = decode_token(token, SECRET)
    assert claims.username == "alice" and claims.role == "admin"


def test_tampered_or_wrong_secret_rejected() -> None:
    token = issue_token("alice", "admin", SECRET, ttl_minutes=5)
    with pytest.raises(TokenError):
        decode_token(token, "t" * 40)
    with pytest.raises(TokenError):
        decode_token(token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1], SECRET)


def test_expired_token_rejected() -> None:
    token = issue_token("alice", "admin", SECRET, ttl_minutes=-1)
    with pytest.raises(TokenError, match="expired"):
        decode_token(token, SECRET)
    assert time.time()  # keep import used
