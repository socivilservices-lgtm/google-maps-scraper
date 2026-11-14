"""Password hashing. Bcrypt only; the hash is what gets stored."""

from __future__ import annotations

import bcrypt

_MAX_BYTES = 72


def hash_password(password: str) -> str:
    digest = password.encode("utf-8")[:_MAX_BYTES]
    return bcrypt.hashpw(digest, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:_MAX_BYTES], password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False
