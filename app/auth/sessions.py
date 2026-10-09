"""Server-side session store - app.db.Session rows, looked up by the opaque
token carried in the session cookie. Deliberately revocable (logout deletes
the row outright) rather than a stateless signed token that would keep
"working" after logout until its own expiry.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import Database, Session as SessionRecord, User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def create_session(db: Database, user_id: int, settings: Settings) -> str:
    token = secrets.token_urlsafe(32)
    async with db.session() as session:
        session.add(SessionRecord(token=token, user_id=user_id, expires_at=_utcnow() + timedelta(hours=settings.session_ttl_hours)))
        await session.commit()
    return token


async def get_user_for_token(session: AsyncSession, token: str) -> User | None:
    record = await session.get(SessionRecord, token)
    if record is None:
        return None

    expires_at = record.expires_at if record.expires_at.tzinfo else record.expires_at.replace(tzinfo=timezone.utc)
    if expires_at < _utcnow():
        await session.execute(delete(SessionRecord).where(SessionRecord.token == token))
        await session.commit()
        return None

    return await session.get(User, record.user_id)


async def delete_session(db: Database, token: str) -> None:
    async with db.session() as session:
        await session.execute(delete(SessionRecord).where(SessionRecord.token == token))
        await session.commit()
