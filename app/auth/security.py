"""Password hashing - bcrypt, a single well-vetted function rather than a
hand-rolled PBKDF2/HMAC scheme. No new abstraction over it: callers use
these two functions directly.
"""
from __future__ import annotations

import bcrypt


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        # Malformed/foreign hash (e.g. a blank string) - never a match, never
        # a raised error a caller would have to remember to catch.
        return False
