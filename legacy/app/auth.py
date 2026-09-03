"""API bearer-token auth for endpoints that expose real catalog/violation
data or can trigger a live GMC action.

Uses constant-time comparison so response timing can't be used to guess
the token. When API_AUTH_TOKEN isn't set (pure local/mock development),
this is a deliberate no-op — `load_settings` already logs a loud warning
in that case whenever a live store or GMC is configured.
"""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request, status

from app.config import Settings


async def require_api_auth(request: Request, authorization: str | None = Header(default=None)) -> None:
    settings: Settings = request.app.state.settings
    if not settings.api_auth_enabled:
        return

    expected = f"Bearer {settings.api_auth_token}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid API token")
