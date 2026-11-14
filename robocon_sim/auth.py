"""Local accounts: password hashing, sessions and role checks.

No third-party dependency and no external identity provider: this is a loopback
tool for one lab. Passwords are stored as scrypt hashes with a per-user salt, and
a session token is a random string kept in SQLite so a restart keeps people
signed in until it expires.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

# ~30-60 ms per hash on a laptop; enough to make offline guessing expensive without
# making login feel slow.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
DKLEN = 32
SESSION_TTL_S = 7 * 24 * 3600
MIN_PASSWORD = 8

ROLES = ("admin", "member")


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise ValueError(f"password must contain at least {MIN_PASSWORD} characters")
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=DKLEN)
    return salt, digest.hex()


def verify_password(password: str, salt: str, expected_hex: str) -> bool:
    if not password or not salt or not expected_hex:
        return False
    try:
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                                n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=DKLEN)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected_hex)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_password(length: int = 12) -> str:
    """Readable but unguessable: no look-alike characters to mistype."""
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def session_expiry(now: float | None = None) -> float:
    return (now if now is not None else time.time()) + SESSION_TTL_S


def check_role(user: dict | None, role: str) -> None:
    if user is None:
        raise PermissionError("login required")
    if user.get("role") != role:
        raise PermissionError(f"{role} role required")
