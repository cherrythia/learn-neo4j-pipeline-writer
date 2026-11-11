"""Password hashing with stdlib scrypt (memory-hard; no extra crypto dependency).

Stored format: ``scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

N, R, P, DKLEN = 2**14, 8, 1, 32
MIN_LENGTH = 12


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def hash_password(password: str) -> str:
    if len(password) < MIN_LENGTH:
        raise ValueError(f"password must be at least {MIN_LENGTH} characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=DKLEN)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(base64.b64decode(expected)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, base64.b64decode(expected))


# Verified against when a username doesn't exist, so timing doesn't reveal valid usernames.
DUMMY_HASH = hash_password("dummy-password-for-timing")
