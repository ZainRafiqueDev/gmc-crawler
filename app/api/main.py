"""FastAPI backend for the frontend (Next.js, app/frontend). Deliberately
minimal - four endpoints matching the frontend's four screens, not a
general-purpose API. Run with:

    uvicorn app.api.main:app --port 8010

Do NOT add --reload on Windows: uvicorn's --reload mode runs the actual
server as a subprocess of its watcher process, and (see
uvicorn/loops/asyncio.py's asyncio_loop_factory) deliberately uses
asyncio.SelectorEventLoop rather than ProactorEventLoop for that subprocess
case specifically. SelectorEventLoop cannot itself spawn subprocesses on
Windows (asyncio.base_events raises NotImplementedError) - and this app's
lifespan (below) launches Playwright's browser at startup, which needs
exactly that. Net effect: --reload makes this app's startup crash
immediately on Windows, every time, with no code-level workaround (setting
the event loop policy at import time does not help - uvicorn's reload
subprocess re-creates the loop itself after import). Confirmed live. Plain
`uvicorn ... --port 8010` (no --reload) uses ProactorEventLoop and starts
fine; you just lose auto-reload-on-code-change during backend development.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response
from playwright.async_api import async_playwright
from sqlalchemy import select

from app.api.jobs import AuditJob, JobStore, critical_count, run_audit_job, run_store_rerun_job, suspension_risk_count
from app.api.schemas import (
    AuditJobStatus,
    AuditRunDetailResponse,
    AuditRunSummary,
    CreateAuditRequest,
    CreateAuditResponse,
    LatestReportResponse,
    MonitoredStoreResponse,
    RegisterStoreRequest,
)
from app.config import load_settings
from app.db import AuditRun, Database, MonitoredStore
from app.graph import PHASE_LABELS
from app.llm.cache import LLMCache
from app.models import Finding
from app.monitor_service import MonitorService
from app.report_docx import markdown_to_docx_bytes
from app.report_pdf import markdown_to_pdf_bytes
from app.scheduling import APSchedulerBackend
from app.security.rate_limiter import RateLimiter
from app.security.ssrf_guard import SSRFBlockedError

logger = logging.getLogger("gmc_audit.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch()

    db = Database(settings.database_url)
    await db.init()

    scheduler = APSchedulerBackend()
    service = MonitorService(db=db, scheduler=scheduler, settings=settings, browser=browser)

    # Resume scheduling for stores already registered (e.g. via monitor.py
    # or a previous server run) - matches `monitor.py serve`'s behavior.
    stores = await service.list_stores()
    for store in stores:
        service.schedule_store(store)
    service.schedule_policy_watch(settings.default_policy_watch_interval_days)
    service.scheduler.start()

    jobs = JobStore(db, retention_days=settings.audit_job_retention_days)
    interrupted = await jobs.mark_interrupted_jobs_as_errored()

    app.state.settings = settings
    app.state.browser = browser
    app.state.service = service
    app.state.jobs = jobs
    app.state.audit_rate_limiter = RateLimiter(settings.audit_rate_limit_max_requests, settings.audit_rate_limit_window_seconds)

    logger.info(
        "API server started (%d store(s) scheduled, %d orphaned job(s) marked as errored from a previous restart)",
        len(stores), interrupted,
    )
    try:
        yield
    finally:
        service.scheduler.shutdown()
        await browser.close()
        await playwright.stop()
        await db.dispose()


app = FastAPI(title="GMC Compliance Checker API", lifespan=lifespan)

# CORSMiddleware is registered at app-definition time, before lifespan runs -
# load settings once here (synchronous, stateless, safe to call again in
# lifespan too) just to get the configured frontend origin.
_cors_settings = load_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_cors_settings.api_cors_origin],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# --- Audits -----------------------------------------------------------

@app.post("/api/audits", response_model=CreateAuditResponse, status_code=202)
async def create_audit(body: CreateAuditRequest, request: Request) -> CreateAuditResponse:
    limiter: RateLimiter = request.app.state.audit_rate_limiter
    if not await limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many audit requests - please wait before trying again.")

    if not body.url or not body.url.strip():
        raise HTTPException(status_code=400, detail="url is required")

    jobs: JobStore = request.app.state.jobs
    job = await jobs.create(body.url.strip())

    # model_copy(update=...) would bypass the hard-cap validators entirely
    # (validate_assignment only fires on direct attribute assignment) - copy
    # first, then assign, exactly like audit.py's CLI flag overrides do.
    run_settings = request.app.state.settings.model_copy()
    if body.max_pages:
        run_settings.crawl_max_pages = body.max_pages
        run_settings.crawl_max_pages_explicit = True
    if body.max_depth:
        run_settings.crawl_max_depth = body.max_depth

    browser = request.app.state.browser
    llm_cache = LLMCache(request.app.state.service.db)
    asyncio.create_task(run_audit_job(job, run_settings, browser, llm_cache, jobs))

    return CreateAuditResponse(job_id=job.job_id)


@app.get("/api/audits/{job_id}", response_model=AuditJobStatus)
async def get_audit_status(job_id: str, request: Request) -> AuditJobStatus:
    jobs: JobStore = request.app.state.jobs
    job: AuditJob | None = await jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    return AuditJobStatus(
        job_id=job.job_id,
        url=job.url,
        status=job.status,
        phase=job.phase,
        phase_label=PHASE_LABELS.get(job.phase) if job.phase else None,
        error=job.error,
        platform=job.platform,
        pages_crawled=job.pages_crawled,
        findings_count=len(job.findings) if job.status == "done" else None,
        critical_count=critical_count(job.findings) if job.status == "done" else None,
        suspension_risk_count=suspension_risk_count(job.findings) if job.status == "done" else None,
        report_markdown=job.report_markdown,
        report_markdown_major_only=job.report_markdown_major_only,
        is_delta=job.is_delta,
        created_at=job.created_at,
    )


def _select_markdown(full: str | None, major_only_variant: str | None, major_only: bool) -> str:
    # Falls back to the full report if a major-only variant isn't available
    # (e.g. a job persisted before this field existed) rather than 404ing or
    # silently serving an empty document.
    if major_only:
        return major_only_variant or full or ""
    return full or ""


@app.get("/api/audits/{job_id}/report.md")
async def download_audit_report_md(job_id: str, request: Request, major_only: bool = False) -> PlainTextResponse:
    job = await _require_done_job(request, job_id)
    return PlainTextResponse(
        _select_markdown(job.report_markdown, job.report_markdown_major_only, major_only),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="report-{job_id}.md"'},
    )


@app.get("/api/audits/{job_id}/report.docx")
async def download_audit_report_docx(job_id: str, request: Request, major_only: bool = False) -> Response:
    job = await _require_done_job(request, job_id)
    docx_bytes = markdown_to_docx_bytes(
        _select_markdown(job.report_markdown, job.report_markdown_major_only, major_only),
        base_dir=request.app.state.settings.report_output_dir,
    )
    return Response(
        docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="report-{job_id}.docx"'},
    )


@app.get("/api/audits/{job_id}/report.pdf")
async def download_audit_report_pdf(job_id: str, request: Request, major_only: bool = False) -> Response:
    job = await _require_done_job(request, job_id)
    pdf_bytes = markdown_to_pdf_bytes(
        _select_markdown(job.report_markdown, job.report_markdown_major_only, major_only),
        base_dir=request.app.state.settings.report_output_dir,
    )
    return Response(
        pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="report-{job_id}.pdf"'},
    )


async def _require_done_job(request: Request, job_id: str) -> AuditJob:
    jobs: JobStore = request.app.state.jobs
    job = await jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "done":
        raise HTTPException(status_code=409, detail=f"job is not done yet (status={job.status})")
    return job


# --- Monitoring ---------------------------------------------------------

@app.post("/api/monitor/stores", response_model=MonitoredStoreResponse, status_code=201)
async def register_store(body: RegisterStoreRequest, request: Request) -> MonitoredStoreResponse:
    service: MonitorService = request.app.state.service
    try:
        store = await service.register_store(
            url=body.url, mode=body.mode,
            interval_days=body.interval_days, cheap_check_interval_days=body.cheap_check_interval_days,
            wc_consumer_key=body.wc_consumer_key, wc_consumer_secret=body.wc_consumer_secret,
            on_policy_change=body.on_policy_change,
        )
    except SSRFBlockedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return MonitoredStoreResponse(
        id=store.id, url=store.url, mode=store.mode,
        interval_days=store.interval_days, cheap_check_interval_days=store.cheap_check_interval_days,
        on_policy_change=store.on_policy_change,
        created_at=store.created_at, last_full_audit_at=store.last_full_audit_at,
        last_cheap_check_at=store.last_cheap_check_at, has_report=False,
    )


@app.get("/api/monitor/stores", response_model=list[MonitoredStoreResponse])
async def list_stores(request: Request) -> list[MonitoredStoreResponse]:
    service: MonitorService = request.app.state.service
    stores = await service.list_stores()

    result: list[MonitoredStoreResponse] = []
    async with service.db.session() as session:
        for store in stores:
            has_report = (await session.execute(
                select(AuditRun.id).where(AuditRun.store_id == store.id, AuditRun.report_markdown.is_not(None)).limit(1)
            )).scalar_one_or_none() is not None
            result.append(MonitoredStoreResponse(
                id=store.id, url=store.url, mode=store.mode,
                interval_days=store.interval_days, cheap_check_interval_days=store.cheap_check_interval_days,
                on_policy_change=store.on_policy_change,
                created_at=store.created_at, last_full_audit_at=store.last_full_audit_at,
                last_cheap_check_at=store.last_cheap_check_at, has_report=has_report,
            ))
    return result


@app.post("/api/monitor/stores/{store_id}/rerun", response_model=CreateAuditResponse, status_code=202)
async def rerun_store_audit(store_id: int, request: Request) -> CreateAuditResponse:
    """On-demand "re-run audit now" for an already-monitored store (Monitored
    Stores / Store Report screens). Goes through the exact same job-creation
    flow as a brand-new ad-hoc audit (same JobStore, same rate limiter) - the
    only difference is the background task drives MonitorService's
    store-monitoring pipeline instead of the bare one, so the result lands
    in that store's AuditRun history and gets diffed into a delta report
    against its previous run, same as a scheduled re-audit would.
    """
    limiter: RateLimiter = request.app.state.audit_rate_limiter
    if not await limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many audit requests - please wait before trying again.")

    service: MonitorService = request.app.state.service
    async with service.db.session() as session:
        store = await session.get(MonitoredStore, store_id)
    if store is None:
        raise HTTPException(status_code=404, detail="store not found")

    jobs: JobStore = request.app.state.jobs
    job = await jobs.create(store.url)
    asyncio.create_task(run_store_rerun_job(job, store_id, service, jobs))

    return CreateAuditResponse(job_id=job.job_id)


@app.delete("/api/monitor/stores/{store_id}", status_code=204)
async def remove_store(store_id: int, request: Request) -> Response:
    service: MonitorService = request.app.state.service
    await service.remove_store(store_id)
    return Response(status_code=204)


async def _get_latest_run_or_404(service: MonitorService, store_id: int) -> AuditRun:
    async with service.db.session() as session:
        store = await session.get(MonitoredStore, store_id)
        if store is None:
            raise HTTPException(status_code=404, detail="store not found")

        run = (await session.execute(
            select(AuditRun)
            .where(AuditRun.store_id == store_id, AuditRun.report_markdown.is_not(None))
            .order_by(AuditRun.started_at.desc())
        )).scalars().first()

    if run is None:
        raise HTTPException(status_code=404, detail="no report available yet for this store")
    return run


@app.get("/api/monitor/stores/{store_id}/latest-report", response_model=LatestReportResponse)
async def latest_report(store_id: int, request: Request) -> LatestReportResponse:
    service: MonitorService = request.app.state.service
    run = await _get_latest_run_or_404(service, store_id)
    findings_count = len(json.loads(run.findings_json)) if run.findings_json else 0

    return LatestReportResponse(
        store_id=store_id, run_type=run.run_type, trigger=run.trigger,
        started_at=run.started_at, finished_at=run.finished_at,
        report_markdown=run.report_markdown, report_markdown_major_only=run.report_markdown_major_only,
        findings_count=findings_count,
    )


@app.get("/api/monitor/stores/{store_id}/latest-report.md")
async def download_latest_report_md(store_id: int, request: Request, major_only: bool = False) -> PlainTextResponse:
    service: MonitorService = request.app.state.service
    run = await _get_latest_run_or_404(service, store_id)
    return PlainTextResponse(
        _select_markdown(run.report_markdown, run.report_markdown_major_only, major_only),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="store-{store_id}-report.md"'},
    )


@app.get("/api/monitor/stores/{store_id}/latest-report.docx")
async def download_latest_report_docx(store_id: int, request: Request, major_only: bool = False) -> Response:
    service: MonitorService = request.app.state.service
    run = await _get_latest_run_or_404(service, store_id)
    docx_bytes = markdown_to_docx_bytes(
        _select_markdown(run.report_markdown, run.report_markdown_major_only, major_only),
        base_dir=service.settings.report_output_dir,
    )
    return Response(
        docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="store-{store_id}-report.docx"'},
    )


@app.get("/api/monitor/stores/{store_id}/latest-report.pdf")
async def download_latest_report_pdf(store_id: int, request: Request, major_only: bool = False) -> Response:
    service: MonitorService = request.app.state.service
    run = await _get_latest_run_or_404(service, store_id)
    pdf_bytes = markdown_to_pdf_bytes(
        _select_markdown(run.report_markdown, run.report_markdown_major_only, major_only),
        base_dir=service.settings.report_output_dir,
    )
    return Response(
        pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="store-{store_id}-report.pdf"'},
    )


@app.get("/api/monitor/stores/{store_id}/runs", response_model=list[AuditRunSummary])
async def list_store_runs(store_id: int, request: Request) -> list[AuditRunSummary]:
    """Audit-history list (Part 2.1) - every retained AuditRun for this
    store, newest first. Only ever as many entries as MonitorService's own
    retention policy actually keeps (audit_run_retention_count) - there is
    no separate "more history exists but isn't shown" state to account for.
    """
    service: MonitorService = request.app.state.service
    async with service.db.session() as session:
        store = await session.get(MonitoredStore, store_id)
        if store is None:
            raise HTTPException(status_code=404, detail="store not found")
    runs = await service.list_runs(store_id)

    result: list[AuditRunSummary] = []
    for run in runs:
        findings = [Finding.model_validate(d) for d in json.loads(run.findings_json)] if run.findings_json else []
        result.append(AuditRunSummary(
            id=run.id, run_type=run.run_type, trigger=run.trigger,
            started_at=run.started_at, finished_at=run.finished_at, change_detected=run.change_detected,
            findings_count=len(findings), critical_count=critical_count(findings),
            suspension_risk_count=suspension_risk_count(findings),
            has_delta=run.delta_markdown is not None,
        ))
    return result


@app.get("/api/monitor/stores/{store_id}/runs/{run_id}", response_model=AuditRunDetailResponse)
async def get_store_run(store_id: int, run_id: int, request: Request) -> AuditRunDetailResponse:
    """Full report (and delta vs. the previous run, if any) for one specific
    historical run - what a history-list entry opens into (Part 2.1)."""
    service: MonitorService = request.app.state.service
    async with service.db.session() as session:
        run = await session.get(AuditRun, run_id)
    if run is None or run.store_id != store_id:
        raise HTTPException(status_code=404, detail="run not found for this store")
    if run.report_markdown is None:
        raise HTTPException(status_code=404, detail="this run has no report (e.g. a cheap_check with no change detected)")

    findings_count = len(json.loads(run.findings_json)) if run.findings_json else 0
    return AuditRunDetailResponse(
        id=run.id, store_id=store_id, run_type=run.run_type, trigger=run.trigger,
        started_at=run.started_at, finished_at=run.finished_at,
        report_markdown=run.report_markdown, report_markdown_major_only=run.report_markdown_major_only,
        delta_markdown=run.delta_markdown, delta_markdown_major_only=run.delta_markdown_major_only,
        findings_count=findings_count,
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
