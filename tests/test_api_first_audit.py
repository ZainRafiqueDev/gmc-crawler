"""Tests for the admin-gated First Audit API: login/logout/me, require_admin
gating, and the PDF report download endpoint. Mounts just the two new
routers into a minimal FastAPI app (same pattern this project uses
elsewhere - test at the function/router level, not the full main.py
lifespan, which launches a real Playwright browser on startup).
"""
from __future__ import annotations

import io

import httpx
import pytest
from fastapi import FastAPI
from pypdf import PdfReader

from app.api import auth as auth_routes
from app.api import first_audit as first_audit_routes
from app.auth.security import hash_password
from app.config import Settings
from app.db import Database, FindingRecord, FirstAuditRun, User


class _FakeService:
    def __init__(self, db: Database) -> None:
        self.db = db


def _make_app(settings: Settings, db: Database) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(first_audit_routes.router)
    app.state.settings = settings
    app.state.service = _FakeService(db)
    app.state.browser = None
    return app


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/api_first_audit_test.db")
    await database.init()
    yield database
    await database.dispose()


@pytest.fixture
def settings():
    return Settings(admin_email="admin@example.com", admin_password="correct-horse-battery-staple")


async def _seed_admin(db: Database, settings: Settings) -> None:
    async with db.session() as session:
        session.add(User(email=settings.admin_email, password_hash=hash_password(settings.admin_password), role="admin"))
        await session.commit()


async def _seed_non_admin(db: Database) -> None:
    async with db.session() as session:
        session.add(User(email="viewer@example.com", password_hash=hash_password("whatever123"), role="viewer"))
        await session.commit()


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_login_with_correct_password_succeeds_and_sets_session(db, settings):
    await _seed_admin(db, settings)
    app = _make_app(settings, db)

    async with await _client(app) as client:
        resp = await client.post("/api/auth/login", json={"email": settings.admin_email, "password": settings.admin_password})
        assert resp.status_code == 200
        assert resp.json() == {"email": settings.admin_email, "role": "admin"}
        assert settings.session_cookie_name in resp.cookies

        me = await client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["email"] == settings.admin_email


@pytest.mark.asyncio
async def test_login_with_wrong_password_is_rejected(db, settings):
    await _seed_admin(db, settings)
    app = _make_app(settings, db)

    async with await _client(app) as client:
        resp = await client.post("/api/auth/login", json={"email": settings.admin_email, "password": "wrong"})
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_logout_invalidates_the_session(db, settings):
    await _seed_admin(db, settings)
    app = _make_app(settings, db)

    async with await _client(app) as client:
        await client.post("/api/auth/login", json={"email": settings.admin_email, "password": settings.admin_password})
        assert (await client.get("/api/auth/me")).status_code == 200

        logout = await client.post("/api/auth/logout")
        assert logout.status_code == 204

        assert (await client.get("/api/auth/me")).status_code == 403


async def _finished_run(db: Database, snapshot_status: str, with_finding: bool) -> int:
    async with db.session() as session:
        run = FirstAuditRun(url="https://example.com", status="done", platform="woocommerce", pages_crawled=12, snapshot_status=snapshot_status)
        session.add(run)
        await session.commit()
        await session.refresh(run)

        if with_finding:
            session.add(FindingRecord(
                audit_run_id=run.id, check_id="missing_returns_page", title="No returns/refund policy page found",
                severity="critical", risk_level="suspension_risk", page_url=None,
                store_evidence="No reachable returns_policy page was found anywhere on the site.",
                google_rule="Return and refund policy must be clearly stated",
                consequence="This may result in potential suspension risk for the whole Merchant Center account.",
                remediation="Publish a dedicated returns/refund policy page.",
                google_source_link="https://support.google.com/merchants/answer/10220642",
            ))
            await session.commit()

        return run.id


@pytest.mark.asyncio
async def test_pdf_download_requires_admin_logged_out_gets_403(db, settings):
    run_id = await _finished_run(db, "CRITICAL", with_finding=True)
    app = _make_app(settings, db)

    async with await _client(app) as client:
        resp = await client.get(f"/api/first-audit/{run_id}/report.pdf")
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_pdf_download_non_admin_user_gets_403(db, settings):
    await _seed_non_admin(db)
    run_id = await _finished_run(db, "CRITICAL", with_finding=True)
    app = _make_app(settings, db)

    async with await _client(app) as client:
        login = await client.post("/api/auth/login", json={"email": "viewer@example.com", "password": "whatever123"})
        assert login.status_code == 200  # valid login, just not an admin

        resp = await client.get(f"/api/first-audit/{run_id}/report.pdf")
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_pdf_download_nonexistent_run_is_404(db, settings):
    await _seed_admin(db, settings)
    app = _make_app(settings, db)

    async with await _client(app) as client:
        await client.post("/api/auth/login", json={"email": settings.admin_email, "password": settings.admin_password})
        resp = await client.get("/api/first-audit/9999/report.pdf")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_pdf_download_unfinished_run_returns_clear_state_not_a_pdf(db, settings):
    await _seed_admin(db, settings)
    async with db.session() as session:
        run = FirstAuditRun(url="https://example.com", status="running")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    app = _make_app(settings, db)
    async with await _client(app) as client:
        await client.post("/api/auth/login", json={"email": settings.admin_email, "password": settings.admin_password})
        resp = await client.get(f"/api/first-audit/{run_id}/report.pdf")
        assert resp.status_code == 409
        assert "running" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_admin_can_download_critical_run_pdf_with_real_finding_content(db, settings):
    await _seed_admin(db, settings)
    run_id = await _finished_run(db, "CRITICAL", with_finding=True)
    app = _make_app(settings, db)

    async with await _client(app) as client:
        await client.post("/api/auth/login", json={"email": settings.admin_email, "password": settings.admin_password})
        resp = await client.get(f"/api/first-audit/{run_id}/report.pdf")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content[:5] == b"%PDF-"

    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(resp.content)).pages)
    assert "CRITICAL" in text
    assert "No returns/refund policy page found" in text
    assert "Return and refund policy must be clearly stated" in text
    assert "Publish a dedicated returns/refund policy page" in text
    assert "https://support.google.com/merchants/answer/10220642" in text  # official Google source link carried through


@pytest.mark.asyncio
async def test_compliant_run_with_no_findings_still_produces_a_valid_pdf(db, settings):
    await _seed_admin(db, settings)
    run_id = await _finished_run(db, "COMPLIANT", with_finding=False)
    app = _make_app(settings, db)

    async with await _client(app) as client:
        await client.post("/api/auth/login", json={"email": settings.admin_email, "password": settings.admin_password})
        resp = await client.get(f"/api/first-audit/{run_id}/report.pdf")

    assert resp.status_code == 200
    assert resp.content[:5] == b"%PDF-"
    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(resp.content)).pages)
    assert "No actionable risks found" in text


# --- Rate limiting / concurrency guards ---------------------------------


@pytest.mark.asyncio
async def test_login_returns_429_after_too_many_failed_attempts_even_with_right_password(db, settings):
    await _seed_admin(db, settings)
    settings.login_rate_limit_ip_max_attempts = 3
    app = _make_app(settings, db)

    async with await _client(app) as client:
        for _ in range(3):
            resp = await client.post("/api/auth/login", json={"email": settings.admin_email, "password": "wrong"})
            assert resp.status_code == 403

        blocked = await client.post("/api/auth/login", json={"email": settings.admin_email, "password": "wrong"})
        assert blocked.status_code == 429
        assert "Too many failed login attempts" in blocked.json()["detail"]
        assert int(blocked.headers["Retry-After"]) > 0

        # locked out - the correct password doesn't bypass the window
        locked = await client.post("/api/auth/login", json={"email": settings.admin_email, "password": settings.admin_password})
        assert locked.status_code == 429


@pytest.mark.asyncio
async def test_login_per_account_limit_applies_independently_of_ip(db, settings):
    await _seed_admin(db, settings)
    settings.login_rate_limit_ip_max_attempts = 100
    settings.login_rate_limit_account_max_attempts = 2
    app = _make_app(settings, db)

    async with await _client(app) as client:
        for _ in range(2):
            assert (await client.post("/api/auth/login", json={"email": settings.admin_email, "password": "wrong"})).status_code == 403
        # same account, differently-cased email - still the same account key
        resp = await client.post("/api/auth/login", json={"email": settings.admin_email.upper(), "password": "wrong"})
        assert resp.status_code == 429


@pytest.mark.asyncio
async def test_successful_login_resets_the_failed_attempt_count(db, settings):
    await _seed_admin(db, settings)
    settings.login_rate_limit_ip_max_attempts = 3
    app = _make_app(settings, db)

    async with await _client(app) as client:
        for _ in range(2):
            await client.post("/api/auth/login", json={"email": settings.admin_email, "password": "wrong"})
        assert (await client.post("/api/auth/login", json={"email": settings.admin_email, "password": settings.admin_password})).status_code == 200
        for _ in range(2):
            assert (await client.post("/api/auth/login", json={"email": settings.admin_email, "password": "wrong"})).status_code == 403


@pytest.fixture
def blocking_runs(monkeypatch):
    """Replaces the real pipeline with one that blocks until released, and
    skips the SSRF guard's DNS lookup - so runs stay "in flight" on demand.
    """
    import asyncio

    release = asyncio.Event()
    started: list[str] = []

    async def _fake_run(run_id, url, settings, browser, db):
        started.append(url)
        await release.wait()

    async def _no_ssrf(url):
        return None

    monkeypatch.setattr(first_audit_routes, "run_first_audit", _fake_run)
    monkeypatch.setattr(first_audit_routes, "assert_public_url", _no_ssrf)
    return release, started


async def _logged_in(client: httpx.AsyncClient, settings: Settings) -> None:
    assert (await client.post("/api/auth/login", json={"email": settings.admin_email, "password": settings.admin_password})).status_code == 200


@pytest.mark.asyncio
async def test_first_audit_rejects_runs_beyond_the_concurrency_cap(db, settings, blocking_runs):
    import asyncio

    release, _started = blocking_runs
    await _seed_admin(db, settings)
    settings.first_audit_max_concurrent = 2
    app = _make_app(settings, db)

    async with await _client(app) as client:
        await _logged_in(client, settings)
        assert (await client.post("/api/first-audit", json={"url": "https://a.example.com"})).status_code == 202
        assert (await client.post("/api/first-audit", json={"url": "https://b.example.com"})).status_code == 202

        over = await client.post("/api/first-audit", json={"url": "https://c.example.com"})
        assert over.status_code == 429
        assert "Too many audits in progress (max 2)" in over.json()["detail"]

        # once in-flight runs finish, their slots free up
        release.set()
        await asyncio.gather(*app.state.first_audit_slots.tasks)
        assert (await client.post("/api/first-audit", json={"url": "https://c.example.com"})).status_code == 202
        await asyncio.gather(*app.state.first_audit_slots.tasks)


@pytest.mark.asyncio
async def test_first_audit_rejects_a_duplicate_run_for_a_url_in_progress(db, settings, blocking_runs):
    import asyncio

    release, _started = blocking_runs
    await _seed_admin(db, settings)
    app = _make_app(settings, db)

    async with await _client(app) as client:
        await _logged_in(client, settings)
        first = await client.post("/api/first-audit", json={"url": "https://shop.example.com/"})
        assert first.status_code == 202
        run_id = first.json()["run_id"]

        # same store, different spelling - still a duplicate
        dup = await client.post("/api/first-audit", json={"url": "HTTP://Shop.Example.com"})
        assert dup.status_code == 409
        assert f"run_id={run_id}" in dup.json()["detail"]

        release.set()
        await asyncio.gather(*app.state.first_audit_slots.tasks)
        assert (await client.post("/api/first-audit", json={"url": "https://shop.example.com"})).status_code == 202
        await asyncio.gather(*app.state.first_audit_slots.tasks)


@pytest.mark.asyncio
async def test_hung_run_times_out_frees_its_slot_and_is_marked_error(db, settings, monkeypatch):
    import asyncio

    async def _hang_forever(run_id, url, settings, browser, db):
        await asyncio.Event().wait()  # e.g. Playwright stuck on a page - never finishes, never raises

    async def _no_ssrf(url):
        return None

    monkeypatch.setattr(first_audit_routes, "run_first_audit", _hang_forever)
    monkeypatch.setattr(first_audit_routes, "assert_public_url", _no_ssrf)

    await _seed_admin(db, settings)
    settings.first_audit_max_concurrent = 1
    settings.first_audit_timeout_seconds = 0.2
    app = _make_app(settings, db)

    async with await _client(app) as client:
        await _logged_in(client, settings)
        resp = await client.post("/api/first-audit", json={"url": "https://stuck.example.com"})
        assert resp.status_code == 202
        run_id = resp.json()["run_id"]

        # the cap of 1 is held while the run hangs
        assert (await client.post("/api/first-audit", json={"url": "https://other.example.com"})).status_code == 429

        await asyncio.gather(*app.state.first_audit_slots.tasks)

        status = (await client.get(f"/api/first-audit/{run_id}")).json()
        assert status["status"] == "error"
        assert "timed out" in status["error"]

        # slot freed - the same URL and a new one are both accepted again
        assert (await client.post("/api/first-audit", json={"url": "https://stuck.example.com"})).status_code == 202
        leftover = list(app.state.first_audit_slots.tasks)
        for task in leftover:
            task.cancel()
        await asyncio.gather(*leftover, return_exceptions=True)


# --- Website-only scope limit (standing product truth) -------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_status,with_finding", [("CRITICAL", True), ("COMPLIANT", False)])
async def test_every_pdf_states_the_website_only_limit(db, settings, snapshot_status, with_finding):
    await _seed_admin(db, settings)
    run_id = await _finished_run(db, snapshot_status, with_finding=with_finding)
    app = _make_app(settings, db)

    async with await _client(app) as client:
        await _logged_in(client, settings)
        resp = await client.get(f"/api/first-audit/{run_id}/report.pdf")

    assert resp.status_code == 200
    text = " ".join("\n".join(page.extract_text() for page in PdfReader(io.BytesIO(resp.content)).pages).split())
    assert "Scope limit: website only - not the feed or account" in text
    assert "does NOT check the Merchant Center product feed or the Merchant Center account" in text
    assert "GTIN" in text
    assert "A clean result here does not mean the account is clean." in text
    if with_finding:
        assert "No returns/refund policy page found" in text
    else:
        assert "No actionable risks found" in text


def test_website_only_limit_comes_before_any_verdict():
    from app.first_audit_report import WEBSITE_ONLY_LIMIT, render_first_audit_report_markdown

    run = FirstAuditRun(url="https://example.com", status="done", snapshot_status="COMPLIANT", pages_crawled=5)
    markdown = render_first_audit_report_markdown(run, [])
    assert WEBSITE_ONLY_LIMIT in markdown
    assert markdown.index(WEBSITE_ONLY_LIMIT) < markdown.index("## No actionable risks found")
