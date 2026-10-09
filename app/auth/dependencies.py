"""FastAPI dependency gating every admin-only route (first-audit + GMC bot
routes, per spec section 6). Logged-out and non-admin both resolve to the
same 403 on the API (no 401/403 distinction - the spec's own wording: "Non-
admin or logged-out = 403 (API) / redirect to login (UI)" treats them as one
case for API purposes; a UI's own redirect-to-login behavior is a frontend
concern, not this dependency's).
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from app.auth.sessions import get_user_for_token
from app.db import Database, User

_FORBIDDEN = HTTPException(status_code=403, detail="Admin access required")


async def require_admin(request: Request) -> User:
    settings = request.app.state.settings
    db: Database = request.app.state.service.db

    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise _FORBIDDEN

    async with db.session() as session:
        user = await get_user_for_token(session, token)

    if user is None or user.role != "admin":
        raise _FORBIDDEN

    return user
