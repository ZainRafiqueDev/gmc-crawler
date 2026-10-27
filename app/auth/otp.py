"""One-time codes for a logged-in admin changing their own password or
email (app/api/auth.py's password/email request-otp + confirm routes) - a
fresh OtpChallenge row per request. A previous unconsumed challenge for the
same (user, purpose) is invalidated first, so only the most recently
emailed code is ever valid - an older email can't be replayed after a
newer code was requested.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import hash_password, verify_password
from app.config import Settings
from app.db import OtpChallenge

# Independent of the request-rate limiter in app/config.py
# (otp_verify_rate_limit_*), which bounds how often a confirm call can be
# made at all - this bounds how many WRONG codes one single outstanding
# challenge tolerates before it's deleted outright, regardless of its own
# TTL. Two different attack shapes: hammering the endpoint vs. guessing
# the 6-digit space against one still-valid code.
_MAX_VERIFY_ATTEMPTS = 5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _generate_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


async def create_otp_challenge(
    session: AsyncSession, user_id: int, purpose: str, settings: Settings, new_email: str | None = None,
) -> str:
    await session.execute(
        delete(OtpChallenge).where(
            OtpChallenge.user_id == user_id, OtpChallenge.purpose == purpose, OtpChallenge.consumed_at.is_(None),
        )
    )
    code = _generate_code()
    session.add(OtpChallenge(
        user_id=user_id, purpose=purpose, code_hash=hash_password(code), new_email=new_email,
        expires_at=_utcnow() + timedelta(minutes=settings.otp_ttl_minutes),
    ))
    await session.commit()
    return code


async def verify_otp_challenge(session: AsyncSession, user_id: int, purpose: str, code: str) -> OtpChallenge | None:
    """Returns the matched challenge (marked consumed) on a correct,
    unexpired code; None otherwise - callers never see why it failed
    (expired/wrong/missing all collapse to the same "invalid or expired
    code" outside this function), matching login's own
    doesn't-distinguish-the-failure-reason discipline.
    """
    challenge = (await session.execute(
        select(OtpChallenge)
        .where(OtpChallenge.user_id == user_id, OtpChallenge.purpose == purpose, OtpChallenge.consumed_at.is_(None))
        .order_by(OtpChallenge.created_at.desc())
    )).scalars().first()

    if challenge is None:
        return None

    expires_at = challenge.expires_at if challenge.expires_at.tzinfo else challenge.expires_at.replace(tzinfo=timezone.utc)
    if expires_at < _utcnow():
        await session.delete(challenge)
        await session.commit()
        return None

    if not verify_password(code, challenge.code_hash):
        challenge.attempts += 1
        if challenge.attempts >= _MAX_VERIFY_ATTEMPTS:
            await session.delete(challenge)
        await session.commit()
        return None

    challenge.consumed_at = _utcnow()
    await session.commit()
    return challenge
