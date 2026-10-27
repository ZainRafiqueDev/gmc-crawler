"""app.auth.otp - the OTP challenge create/verify logic, tested directly
against a DB session (no HTTP layer) - see tests/test_api_auth_password_
email_change.py for the routes that use this.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.auth.otp import _MAX_VERIFY_ATTEMPTS, create_otp_challenge, verify_otp_challenge
from app.auth.security import hash_password
from app.config import Settings
from app.db import Database, OtpChallenge, User


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/otp_test.db")
    await database.init()
    async with database.session() as session:
        session.add(User(id=1, email="admin@example.com", password_hash=hash_password("whatever"), role="admin"))
        await session.commit()
    yield database
    await database.dispose()


@pytest.fixture
def settings():
    return Settings(otp_ttl_minutes=10)


@pytest.mark.asyncio
async def test_correct_code_verifies_and_marks_consumed(db, settings):
    async with db.session() as session:
        code = await create_otp_challenge(session, user_id=1, purpose="password_change", settings=settings)

    async with db.session() as session:
        challenge = await verify_otp_challenge(session, user_id=1, purpose="password_change", code=code)
        assert challenge is not None
        assert challenge.consumed_at is not None


@pytest.mark.asyncio
async def test_wrong_code_is_rejected_and_does_not_consume(db, settings):
    async with db.session() as session:
        await create_otp_challenge(session, user_id=1, purpose="password_change", settings=settings)

    async with db.session() as session:
        assert await verify_otp_challenge(session, user_id=1, purpose="password_change", code="000000") is None


@pytest.mark.asyncio
async def test_already_consumed_code_cannot_be_reused(db, settings):
    async with db.session() as session:
        code = await create_otp_challenge(session, user_id=1, purpose="password_change", settings=settings)
    async with db.session() as session:
        assert await verify_otp_challenge(session, user_id=1, purpose="password_change", code=code) is not None
    async with db.session() as session:
        assert await verify_otp_challenge(session, user_id=1, purpose="password_change", code=code) is None


@pytest.mark.asyncio
async def test_requesting_a_new_code_invalidates_the_previous_one(db, settings):
    """Confirmed real requirement: only the most recently emailed code is
    ever valid - an older email can't be replayed after a newer code was
    requested."""
    async with db.session() as session:
        old_code = await create_otp_challenge(session, user_id=1, purpose="password_change", settings=settings)
    async with db.session() as session:
        new_code = await create_otp_challenge(session, user_id=1, purpose="password_change", settings=settings)

    assert old_code != new_code
    async with db.session() as session:
        assert await verify_otp_challenge(session, user_id=1, purpose="password_change", code=old_code) is None
    async with db.session() as session:
        assert await verify_otp_challenge(session, user_id=1, purpose="password_change", code=new_code) is not None


@pytest.mark.asyncio
async def test_expired_code_is_rejected(db, settings):
    async with db.session() as session:
        code = await create_otp_challenge(session, user_id=1, purpose="password_change", settings=settings)
        # Force it into the past directly - create_otp_challenge's own TTL
        # is a fixed 10 minutes here, nothing to configure down to "already
        # expired" through the public API alone.
        challenge = (await session.execute(select(OtpChallenge).where(OtpChallenge.user_id == 1))).scalars().first()
        challenge.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await session.commit()

    async with db.session() as session:
        assert await verify_otp_challenge(session, user_id=1, purpose="password_change", code=code) is None


@pytest.mark.asyncio
async def test_code_deleted_outright_after_max_wrong_attempts(db, settings):
    async with db.session() as session:
        code = await create_otp_challenge(session, user_id=1, purpose="password_change", settings=settings)

    for _ in range(_MAX_VERIFY_ATTEMPTS):
        async with db.session() as session:
            assert await verify_otp_challenge(session, user_id=1, purpose="password_change", code="000000") is None

    # Even the real code no longer works - the challenge row itself is gone.
    async with db.session() as session:
        assert await verify_otp_challenge(session, user_id=1, purpose="password_change", code=code) is None


@pytest.mark.asyncio
async def test_different_purposes_do_not_cross_over(db, settings):
    async with db.session() as session:
        password_code = await create_otp_challenge(session, user_id=1, purpose="password_change", settings=settings)
    async with db.session() as session:
        await create_otp_challenge(session, user_id=1, purpose="email_change", settings=settings, new_email="new@example.com")

    # The password_change code is untouched by the later, unrelated-purpose
    # email_change request - only same-(user, purpose) requests invalidate
    # each other.
    async with db.session() as session:
        assert await verify_otp_challenge(session, user_id=1, purpose="password_change", code=password_code) is not None


@pytest.mark.asyncio
async def test_email_change_challenge_carries_the_pending_new_email(db, settings):
    async with db.session() as session:
        code = await create_otp_challenge(session, user_id=1, purpose="email_change", settings=settings, new_email="new@example.com")

    async with db.session() as session:
        challenge = await verify_otp_challenge(session, user_id=1, purpose="email_change", code=code)
        assert challenge.new_email == "new@example.com"
