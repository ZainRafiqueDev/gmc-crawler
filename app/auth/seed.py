"""Seeds exactly one admin user from ADMIN_EMAIL/ADMIN_PASSWORD on startup
(GMC bot spec section 6) - called once from app.api.main's lifespan, same
timing as Database.init(). Re-running this on every startup keeps the
admin's password in sync with the current env var (the env var is the
source of truth, same pattern as app.rules.loader syncing the YAML into its
DB mirror) - if ADMIN_PASSWORD changes, the next restart picks it up rather
than silently keeping a stale hash around.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.auth.security import hash_password
from app.config import Settings
from app.db import Database, User

logger = logging.getLogger("gmc_audit.auth.seed")


async def seed_admin(db: Database, settings: Settings) -> None:
    if not settings.admin_email or not settings.admin_password:
        logger.warning("ADMIN_EMAIL/ADMIN_PASSWORD not set - no admin user seeded, every admin route will refuse all requests")
        return

    async with db.session() as session:
        existing = (await session.execute(select(User).where(User.email == settings.admin_email))).scalar_one_or_none()
        password_hash = hash_password(settings.admin_password)

        if existing is None:
            session.add(User(email=settings.admin_email, password_hash=password_hash, role="admin"))
            logger.info("Seeded admin user %s", settings.admin_email)
        else:
            existing.password_hash = password_hash
            existing.role = "admin"

        await session.commit()
