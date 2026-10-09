"""Admin-gated First Audit API routes - minimal: create a run, poll its
status, and download its PDF report. Every route here requires require_admin
(GMC bot spec section 6); there is no public, unauthenticated route for any
of this, unlike the old pipeline's /api/audits.

POST is guarded by an in-process run registry (_FirstAuditSlots): at most
Settings.first_audit_max_concurrent runs in flight at once (429 beyond it -
the OOM guard against unbounded concurrent Playwright+LLM crawls), and at
most one in-flight run per URL (409 for a duplicate). In-memory rather than
derived from FirstAuditRun.status, so a run left "running" by a crashed
process can never wedge the cap shut.

SINGLE-WORKER ONLY: the registry is per-process. Under `uvicorn --workers N`
the cap silently becomes N x first_audit_max_concurrent and the duplicate
guard only sees its own worker's runs - nothing fails loudly. Move the
registry to Redis before running more than one worker.

Every run is wrapped in Settings.first_audit_timeout_seconds, so a hung
crawl times out, is marked "error", and frees its slot instead of holding
it until restart.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from app.auth.dependencies import require_admin
from app.config import Settings
from app.db import Database, FindingRecord, FirstAuditRun, User
from app.first_audit import _mark_error, create_first_audit_run, get_first_audit_run, run_first_audit
from app.first_audit_report import render_first_audit_report_markdown
from app.report_pdf import markdown_to_pdf_bytes
from app.security.ssrf_guard import SSRFBlockedError, assert_public_url

router = APIRouter(prefix="/api/first-audit", tags=["first-audit"])
logger = logging.getLogger("gmc_audit.api.first_audit")


class CreateFirstAuditRequest(BaseModel):
    url: str


class CreateFirstAuditResponse(BaseModel):
    run_id: int


class FirstAuditStatusResponse(BaseModel):
    run_id: int
    url: str
    status: str
    platform: str | None
    pages_crawled: int | None
    unreachable_pages_count: int | None
    snapshot_status: str | None
    error: str | None


def _url_key(url: str) -> str:
    """Duplicate-run key: scheme-insensitive, host lowercased, trailing
    slash dropped - so example.com, https://Example.com/ and
    http://example.com are all the same in-flight store.
    """
    parts = urlsplit(url if "://" in url else f"https://{url}")
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port else ""
    query = f"?{parts.query}" if parts.query else ""
    return f"{host}{port}{parts.path.rstrip('/')}{query}"


class _FirstAuditSlots:
    def __init__(self, max_concurrent: int) -> None:
        self.max_concurrent = max_concurrent
        self._run_id_by_url_key: dict[str, int | None] = {}
        self._lock = asyncio.Lock()
        # Strong refs to the background tasks - the event loop only keeps a
        # weak one, and a GC'd task would never reach release().
        self.tasks: set[asyncio.Task] = set()

    async def reserve(self, url_key: str) -> None:
        """Raises 409 for a URL already in flight, 429 at the concurrency cap."""
        async with self._lock:
            if url_key in self._run_id_by_url_key:
                existing = self._run_id_by_url_key[url_key]
                detail = "An audit for this URL is already in progress"
                raise HTTPException(status_code=409, detail=f"{detail} (run_id={existing})." if existing is not None else f"{detail}.")
            if len(self._run_id_by_url_key) >= self.max_concurrent:
                raise HTTPException(
                    status_code=429,
                    detail=f"Too many audits in progress (max {self.max_concurrent}) - wait for one to finish and try again.",
                )
            self._run_id_by_url_key[url_key] = None

    async def attach(self, url_key: str, run_id: int) -> None:
        async with self._lock:
            self._run_id_by_url_key[url_key] = run_id

    async def release(self, url_key: str) -> None:
        async with self._lock:
            self._run_id_by_url_key.pop(url_key, None)


def _first_audit_slots(request: Request) -> _FirstAuditSlots:
    state = request.app.state
    if getattr(state, "first_audit_slots", None) is None:
        state.first_audit_slots = _FirstAuditSlots(state.settings.first_audit_max_concurrent)
    return state.first_audit_slots


async def _run_and_release(slots: _FirstAuditSlots, url_key: str, run_id: int, url: str, settings: Settings, browser, db: Database) -> None:
    try:
        await asyncio.wait_for(run_first_audit(run_id, url, settings, browser, db), timeout=settings.first_audit_timeout_seconds)
    except asyncio.TimeoutError:
        logger.error("First audit %d timed out after %ss - marking error and freeing its slot", run_id, settings.first_audit_timeout_seconds)
        await _mark_error(db, run_id, f"Audit timed out after {settings.first_audit_timeout_seconds:g}s and was stopped.")
    finally:
        await slots.release(url_key)


@router.post("", response_model=CreateFirstAuditResponse, status_code=202)
async def create_first_audit(body: CreateFirstAuditRequest, request: Request, _admin: User = Depends(require_admin)) -> CreateFirstAuditResponse:
    if not body.url or not body.url.strip():
        raise HTTPException(status_code=400, detail="url is required")

    url = body.url.strip()
    try:
        await assert_public_url(url if "://" in url else f"https://{url}")
    except SSRFBlockedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    settings = request.app.state.settings
    browser = request.app.state.browser
    db = request.app.state.service.db

    slots = _first_audit_slots(request)
    url_key = _url_key(url)
    await slots.reserve(url_key)
    try:
        run_id = await create_first_audit_run(db, url)
        await slots.attach(url_key, run_id)
    except BaseException:
        await slots.release(url_key)
        raise

    task = asyncio.create_task(_run_and_release(slots, url_key, run_id, url, settings, browser, db))
    slots.tasks.add(task)
    task.add_done_callback(slots.tasks.discard)
    return CreateFirstAuditResponse(run_id=run_id)


async def _require_run(request: Request, run_id: int) -> FirstAuditRun:
    db = request.app.state.service.db
    run = await get_first_audit_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="first-audit run not found")
    return run


@router.get("/{run_id}", response_model=FirstAuditStatusResponse)
async def get_first_audit_status(run_id: int, request: Request, _admin: User = Depends(require_admin)) -> FirstAuditStatusResponse:
    run = await _require_run(request, run_id)
    return FirstAuditStatusResponse(
        run_id=run.id, url=run.url, status=run.status, platform=run.platform,
        pages_crawled=run.pages_crawled, unreachable_pages_count=run.unreachable_pages_count,
        snapshot_status=run.snapshot_status, error=run.error,
    )


@router.get("/{run_id}/report.pdf")
async def download_first_audit_report_pdf(run_id: int, request: Request, _admin: User = Depends(require_admin)) -> Response:
    """Rendered on demand from the normalized FindingRecord rows every time
    - no PDF is cached/stored per run. Chosen deliberately: rendering this
    Markdown subset through reportlab is pure CPU, no network/LLM calls, and
    takes a fraction of a second even for a large finding set, so caching
    would only add Tier-2-evidence-style storage/retention complexity (what
    this project's evidence_retention round exists to manage for raw
    crawl/screenshot data) for no real latency win. If that changes (a much
    heavier rendering step, or real demand for a stable shareable link),
    caching would need to go through the same evidence store + retention
    policy as everything else in Tier 2 - never an unbounded side table.
    """
    run = await _require_run(request, run_id)

    if run.status != "done":
        raise HTTPException(status_code=409, detail=f"audit is not finished yet (status={run.status})")

    db = request.app.state.service.db
    async with db.session() as session:
        findings = (await session.execute(
            select(FindingRecord).where(FindingRecord.audit_run_id == run_id)
        )).scalars().all()

    markdown = render_first_audit_report_markdown(run, findings)
    pdf_bytes = markdown_to_pdf_bytes(markdown, base_dir=request.app.state.settings.report_output_dir)

    return Response(
        pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="first-audit-{run_id}-report.pdf"'},
    )
