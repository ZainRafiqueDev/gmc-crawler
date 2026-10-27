"""HTTP-level tests for the admin self-service password/email change routes
(app/api/auth.py's password/* and email/* routes) - same minimal-app
pattern as tests/test_api_first_audit.py. send_otp_email is monkeypatched
(no real SMTP in tests) to a fake that records what it was asked to send,
so tests can assert the OTP went to the right address without a network call.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.api import auth as auth_routes
from app.auth.security import hash_password
from app.config import Settings
from app.db import Database, User


class _FakeService:
    def __init__(self, db: Database) -> None:
        self.db = db


class _SentEmail:
    def __init__(self, to_email: str, code: str, purpose: str) -> None:
        self.to_email = to_email
        self.code = code
        self.purpose = purpose


class _FakeOutbox:
    def __init__(self) -> None:
        self.sent: list[_SentEmail] = []

    def send(self, to_email: str, code: str, purpose: str, settings) -> None:
        self.sent.append(_SentEmail(to_email, code, purpose))


def _make_app(settings: Settings, db: Database, outbox: _FakeOutbox) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.state.settings = settings
    app.state.service = _FakeService(db)

    async def _fake_send_otp_or_raise(to_email, code, purpose, settings):
        outbox.send(to_email, code, purpose, settings)

    auth_routes._send_otp_or_raise = _fake_send_otp_or_raise  # type: ignore[attr-defined]
    return app


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/api_otp_test.db")
    await database.init()
    yield database
    await database.dispose()


@pytest.fixture
def settings():
    return Settings(admin_email="admin@example.com", admin_password="correct-horse-battery-staple")


@pytest.fixture
def outbox():
    return _FakeOutbox()


async def _seed_admin(db: Database, settings: Settings) -> None:
    async with db.session() as session:
        session.add(User(email=settings.admin_email, password_hash=hash_password(settings.admin_password), role="admin"))
        await session.commit()


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _login(client: httpx.AsyncClient, settings: Settings) -> None:
    resp = await client.post("/api/auth/login", json={"email": settings.admin_email, "password": settings.admin_password})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_password_change_full_flow_succeeds_and_invalidates_old_session(db, settings, outbox):
    await _seed_admin(db, settings)
    app = _make_app(settings, db, outbox)

    async with _client(app) as client:
        await _login(client, settings)
        resp = await client.post("/api/auth/password/request-otp", json={"current_password": settings.admin_password})
        assert resp.status_code == 204
        assert len(outbox.sent) == 1
        assert outbox.sent[0].to_email == settings.admin_email
        assert outbox.sent[0].purpose == "password_change"
        code = outbox.sent[0].code

        resp = await client.post(
            "/api/auth/password/confirm",
            json={"current_password": settings.admin_password, "otp": code, "new_password": "a-new-password-123"},
        )
        assert resp.status_code == 200

        # The session used to make this very request is invalidated too -
        # the next call with the same cookie jar 403s.
        me = await client.get("/api/auth/me")
        assert me.status_code == 403

    # New password actually works; old one no longer does.
    client2 = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with client2 as client:
        resp = await client.post("/api/auth/login", json={"email": settings.admin_email, "password": "a-new-password-123"})
        assert resp.status_code == 200
        resp = await client.post("/api/auth/login", json={"email": settings.admin_email, "password": settings.admin_password})
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_password_request_otp_rejects_wrong_current_password(db, settings, outbox):
    await _seed_admin(db, settings)
    app = _make_app(settings, db, outbox)

    async with _client(app) as client:
        await _login(client, settings)
        resp = await client.post("/api/auth/password/request-otp", json={"current_password": "wrong"})
        assert resp.status_code == 403
        assert outbox.sent == []


@pytest.mark.asyncio
async def test_password_confirm_rejects_wrong_otp(db, settings, outbox):
    await _seed_admin(db, settings)
    app = _make_app(settings, db, outbox)

    async with _client(app) as client:
        await _login(client, settings)
        await client.post("/api/auth/password/request-otp", json={"current_password": settings.admin_password})
        resp = await client.post(
            "/api/auth/password/confirm",
            json={"current_password": settings.admin_password, "otp": "000000", "new_password": "a-new-password-123"},
        )
        assert resp.status_code == 403
        # Session survives a failed attempt - only a SUCCESSFUL change
        # invalidates it.
        me = await client.get("/api/auth/me")
        assert me.status_code == 200


@pytest.mark.asyncio
async def test_password_confirm_rejects_too_short_new_password(db, settings, outbox):
    await _seed_admin(db, settings)
    app = _make_app(settings, db, outbox)

    async with _client(app) as client:
        await _login(client, settings)
        await client.post("/api/auth/password/request-otp", json={"current_password": settings.admin_password})
        code = outbox.sent[0].code
        resp = await client.post(
            "/api/auth/password/confirm",
            json={"current_password": settings.admin_password, "otp": code, "new_password": "short"},
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_email_change_full_flow_sends_otp_to_old_address_and_updates_email(db, settings, outbox):
    await _seed_admin(db, settings)
    app = _make_app(settings, db, outbox)

    async with _client(app) as client:
        await _login(client, settings)
        resp = await client.post(
            "/api/auth/email/request-otp",
            json={"current_password": settings.admin_password, "new_email": "new-admin@example.com"},
        )
        assert resp.status_code == 204
        # Confirmed requirement: the OTP goes to the OLD/current email, not
        # the one being requested.
        assert outbox.sent[0].to_email == settings.admin_email
        assert outbox.sent[0].purpose == "email_change"
        code = outbox.sent[0].code

        resp = await client.post(
            "/api/auth/email/confirm", json={"current_password": settings.admin_password, "otp": code},
        )
        assert resp.status_code == 200
        assert resp.json()["email"] == "new-admin@example.com"

        # Old session invalidated here too.
        me = await client.get("/api/auth/me")
        assert me.status_code == 403


@pytest.mark.asyncio
async def test_email_request_otp_rejects_an_email_already_in_use(db, settings, outbox):
    await _seed_admin(db, settings)
    async with db.session() as session:
        session.add(User(email="taken@example.com", password_hash=hash_password("whatever123"), role="admin"))
        await session.commit()
    app = _make_app(settings, db, outbox)

    async with _client(app) as client:
        await _login(client, settings)
        resp = await client.post(
            "/api/auth/email/request-otp",
            json={"current_password": settings.admin_password, "new_email": "taken@example.com"},
        )
        assert resp.status_code == 400
        assert outbox.sent == []


@pytest.mark.asyncio
async def test_otp_routes_require_login(db, settings, outbox):
    await _seed_admin(db, settings)
    app = _make_app(settings, db, outbox)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/auth/password/request-otp", json={"current_password": "whatever"})
        assert resp.status_code == 403
