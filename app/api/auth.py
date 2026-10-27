"""Admin login/logout/me (GMC bot spec section 6) - a session cookie, not a
bearer token, since the only client today is this project's own frontend.
Also the admin's own self-service password/email change, OTP-gated
(app.auth.otp, app.auth.email) - see that module pair's own docstrings for
the OTP challenge/email-sending design.

Cookie is always httponly; `secure`/`samesite` come from
Settings.session_cookie_secure/session_cookie_samesite (default: insecure +
lax, right for local dev where frontend and backend share localhost). A
deployment that puts the frontend and backend on different domains (e.g.
Vercel + Render) is cross-site, which needs SESSION_COOKIE_SECURE=true and
SESSION_COOKIE_SAMESITE=none set in the *backend's* own env - see
app/config.py's own comment on these fields for why.

Login is rate limited on FAILED attempts, per client IP and per submitted
email (Settings.login_rate_limit_*): once either key is at its limit, every
attempt - even one with the right password - gets a 429 until the window
slides. A successful login clears both keys. The password/email change
routes below have their own, separate pair of limiters
(Settings.otp_request_rate_limit_*/otp_verify_rate_limit_*), keyed by admin
id rather than IP/submitted-email since every call there is already
authenticated.

SINGLE-WORKER ONLY: every limiter here lives in process memory, so under
`uvicorn --workers N` every worker counts separately and an attacker gets
N x the attempts. Move the counters to Redis before scaling out.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, select

from app.auth.dependencies import require_admin
from app.auth.email import EmailNotConfiguredError, send_otp_email
from app.auth.otp import create_otp_challenge, verify_otp_challenge
from app.auth.security import hash_password, verify_password
from app.auth.sessions import create_session, delete_session, get_user_for_token
from app.db import Session as SessionRecord, User
from app.security.rate_limiter import RateLimiter

logger = logging.getLogger("gmc_audit.api.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

_MIN_NEW_PASSWORD_LENGTH = 8


class LoginRequest(BaseModel):
    email: str
    password: str


class MeResponse(BaseModel):
    email: str
    role: str


class RequestOtpRequest(BaseModel):
    current_password: str


class ConfirmPasswordChangeRequest(BaseModel):
    current_password: str
    otp: str
    new_password: str


class RequestEmailChangeRequest(BaseModel):
    current_password: str
    new_email: str


class ConfirmEmailChangeRequest(BaseModel):
    current_password: str
    otp: str


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


def _otp_limiters(request: Request) -> tuple[RateLimiter, RateLimiter]:
    """(request, verify) limiters for the password/email change routes -
    same lazy app.state pattern as _login_limiters, keyed by admin id
    (every call here is already authenticated, unlike login).
    """
    state = request.app.state
    if getattr(state, "otp_request_limiter", None) is None:
        settings = state.settings
        state.otp_request_limiter = RateLimiter(settings.otp_request_rate_limit_max_attempts, settings.otp_request_rate_limit_window_seconds)
        state.otp_verify_limiter = RateLimiter(settings.otp_verify_rate_limit_max_attempts, settings.otp_verify_rate_limit_window_seconds)
    return state.otp_request_limiter, state.otp_verify_limiter


async def _send_otp_or_raise(to_email: str, code: str, purpose: str, settings) -> None:
    try:
        send_otp_email(to_email, code, purpose, settings)
    except EmailNotConfiguredError:
        raise HTTPException(status_code=500, detail="Email sending is not configured on this server") from None
    except Exception:
        logger.exception("Failed to send %s OTP email", purpose)
        raise HTTPException(status_code=502, detail="Could not send the verification email - please try again.") from None


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
        settings.session_cookie_name, token, httponly=True,
        secure=settings.session_cookie_secure, samesite=settings.session_cookie_samesite,
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
    response.delete_cookie(
        settings.session_cookie_name, path="/",
        secure=settings.session_cookie_secure, samesite=settings.session_cookie_samesite,
    )
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


@router.post("/password/request-otp", status_code=204)
async def request_password_otp(body: RequestOtpRequest, request: Request, admin: User = Depends(require_admin)) -> Response:
    settings = request.app.state.settings
    db = request.app.state.service.db
    request_limiter, _ = _otp_limiters(request)

    if not await request_limiter.allow(str(admin.id)):
        raise HTTPException(status_code=429, detail="Too many code requests - please wait before trying again.")
    if not verify_password(body.current_password, admin.password_hash):
        raise HTTPException(status_code=403, detail="Current password is incorrect")

    async with db.session() as session:
        code = await create_otp_challenge(session, admin.id, "password_change", settings)

    await _send_otp_or_raise(admin.email, code, "password_change", settings)
    return Response(status_code=204)


@router.post("/password/confirm", response_model=MeResponse)
async def confirm_password_change(body: ConfirmPasswordChangeRequest, request: Request, admin: User = Depends(require_admin)) -> MeResponse:
    db = request.app.state.service.db
    _, verify_limiter = _otp_limiters(request)

    if not await verify_limiter.allow(str(admin.id)):
        raise HTTPException(status_code=429, detail="Too many attempts - please wait before trying again.")
    if not verify_password(body.current_password, admin.password_hash):
        raise HTTPException(status_code=403, detail="Current password is incorrect")
    if len(body.new_password) < _MIN_NEW_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"New password must be at least {_MIN_NEW_PASSWORD_LENGTH} characters")

    async with db.session() as session:
        challenge = await verify_otp_challenge(session, admin.id, "password_change", body.otp)
        if challenge is None:
            raise HTTPException(status_code=403, detail="Invalid or expired code")

        user = await session.get(User, admin.id)
        user.password_hash = hash_password(body.new_password)
        # Every outstanding session for this account - including the one
        # making this request - is invalidated: a changed password should
        # mean every existing cookie (including a stolen one) stops
        # working, not just future logins. The frontend re-logs-in
        # afterward (its next /api/auth/me 403s, same as any other
        # session-ended case).
        await session.execute(delete(SessionRecord).where(SessionRecord.user_id == admin.id))
        await session.commit()
        return MeResponse(email=user.email, role=user.role)


@router.post("/email/request-otp", status_code=204)
async def request_email_change_otp(body: RequestEmailChangeRequest, request: Request, admin: User = Depends(require_admin)) -> Response:
    settings = request.app.state.settings
    db = request.app.state.service.db
    request_limiter, _ = _otp_limiters(request)

    if not await request_limiter.allow(str(admin.id)):
        raise HTTPException(status_code=429, detail="Too many code requests - please wait before trying again.")
    if not verify_password(body.current_password, admin.password_hash):
        raise HTTPException(status_code=403, detail="Current password is incorrect")

    new_email = body.new_email.strip().lower()
    if not new_email or "@" not in new_email:
        raise HTTPException(status_code=400, detail="A valid new email is required")

    async with db.session() as session:
        existing = (await session.execute(select(User).where(User.email == new_email))).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=400, detail="That email is already in use")
        code = await create_otp_challenge(session, admin.id, "email_change", settings, new_email=new_email)

    # Sent to the CURRENT (soon-to-be-old) email, never the new one - the
    # whole point of this flow is confirming the change from an address the
    # admin already controls, not the one being requested.
    await _send_otp_or_raise(admin.email, code, "email_change", settings)
    return Response(status_code=204)


@router.post("/email/confirm", response_model=MeResponse)
async def confirm_email_change(body: ConfirmEmailChangeRequest, request: Request, admin: User = Depends(require_admin)) -> MeResponse:
    db = request.app.state.service.db
    _, verify_limiter = _otp_limiters(request)

    if not await verify_limiter.allow(str(admin.id)):
        raise HTTPException(status_code=429, detail="Too many attempts - please wait before trying again.")
    if not verify_password(body.current_password, admin.password_hash):
        raise HTTPException(status_code=403, detail="Current password is incorrect")

    async with db.session() as session:
        challenge = await verify_otp_challenge(session, admin.id, "email_change", body.otp)
        if challenge is None or challenge.new_email is None:
            raise HTTPException(status_code=403, detail="Invalid or expired code")

        user = await session.get(User, admin.id)
        user.email = challenge.new_email
        await session.execute(delete(SessionRecord).where(SessionRecord.user_id == admin.id))
        await session.commit()
        return MeResponse(email=user.email, role=user.role)
