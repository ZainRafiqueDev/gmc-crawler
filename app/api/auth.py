"""Admin login/logout/me (GMC bot spec section 6) - a session cookie, not a
bearer token, since the only client today is this project's own frontend.

Cookie is httponly + samesite=lax; `secure` is left off since this project's
dev workflow serves the API over plain http://localhost - a production
deployment behind HTTPS should set it (a one-line change here, flagged
rather than silently assumed).

Login is rate limited on FAILED attempts, per client IP and per submitted
email (Settings.login_rate_limit_*): once either key is at its limit, every
attempt - even one with the right password - gets a 429 until the window
slides. A successful login clears both keys.

SINGLE-WORKER ONLY: the limiters live in process memory, so under
`uvicorn --workers N` every worker counts separately and an attacker gets
N x the attempts. Move the counters to Redis before scaling out.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from app.auth.security import verify_password
from app.auth.sessions import create_session, delete_session, get_user_for_token
from app.db import User
from app.security.rate_limiter import RateLimiter

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


class MeResponse(BaseModel):
    email: str
    role: str


def _login_limiters(request: Request) -> tuple[RateLimiter, RateLimiter]:
    """(per-IP, per-account) failed-login limiters, created lazily on
    app.state so both main.py's app and the tests' minimal app get them.
    """
    state = request.app.state
    if getattr(state, "login_ip_limiter", None) is None:
        settings = state.settings
        state.login_ip_limiter = RateLimiter(settings.login_rate_limit_ip_max_attempts, settings.login_rate_limit_ip_window_seconds)
        state.login_account_limiter = RateLimiter(settings.login_rate_limit_account_max_attempts, settings.login_rate_limit_account_window_seconds)
    return state.login_ip_limiter, state.login_account_limiter


@router.post("/login", response_model=MeResponse)
async def login(body: LoginRequest, request: Request, response: Response) -> MeResponse:
    settings = request.app.state.settings
    db = request.app.state.service.db

    ip_limiter, account_limiter = _login_limiters(request)
    ip_key = request.client.host if request.client else "unknown"
    account_key = body.email.strip().lower()

    for limiter, key in ((ip_limiter, ip_key), (account_limiter, account_key)):
        if await limiter.remaining(key) == 0:
            raise HTTPException(
                status_code=429,
                detail="Too many failed login attempts - please wait before trying again.",
                headers={"Retry-After": str(int(limiter.window_seconds))},
            )

    async with db.session() as session:
        user = (await session.execute(select(User).where(User.email == body.email))).scalar_one_or_none()

    if user is None or not verify_password(body.password, user.password_hash):
        await ip_limiter.allow(ip_key)
        await account_limiter.allow(account_key)
        raise HTTPException(status_code=403, detail="Invalid email or password")

    await ip_limiter.reset(ip_key)
    await account_limiter.reset(account_key)

    token = await create_session(db, user.id, settings)
    response.set_cookie(
        settings.session_cookie_name, token, httponly=True, samesite="lax",
        max_age=settings.session_ttl_hours * 3600, path="/",
    )
    return MeResponse(email=user.email, role=user.role)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response) -> Response:
    settings = request.app.state.settings
    db = request.app.state.service.db

    token = request.cookies.get(settings.session_cookie_name)
    if token:
        await delete_session(db, token)
    response.delete_cookie(settings.session_cookie_name, path="/")
    return Response(status_code=204)


@router.get("/me", response_model=MeResponse)
async def me(request: Request) -> MeResponse:
    settings = request.app.state.settings
    db = request.app.state.service.db

    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise HTTPException(status_code=403, detail="Not logged in")

    async with db.session() as session:
        user = await get_user_for_token(session, token)

    if user is None:
        raise HTTPException(status_code=403, detail="Not logged in")

    return MeResponse(email=user.email, role=user.role)
