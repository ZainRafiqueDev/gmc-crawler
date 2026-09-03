"""DB-backed audit job tracking for the frontend's "Run Audit" flow.

Was previously an in-process dict - lost every job (including completed
reports, whose download links then 404'd) on any backend restart. Now
persisted via AuditJobRecord (app/db.py): status, phase, findings, and the
rendered report itself all survive a restart. Verified by killing and
restarting the server mid-session with a completed job on hand and
confirming both downloads still work with no re-run needed.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from playwright.async_api import Browser
from sqlalchemy import select

from app.config import Settings
from app.db import AuditJobRecord, Database
from app.graph import run_audit_streaming
from app.llm.cache import LLMCache
from app.models import Finding, Severity
from app.report import generate_markdown_report, is_suspension_risk_finding
from app.security.ssrf_guard import SSRFBlockedError, assert_public_url

logger = logging.getLogger("gmc_audit.api.jobs")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AuditJob:
    """In-memory view of one AuditJobRecord row - what route handlers and
    run_audit_job actually work with; JobStore reads/writes the DB row
    underneath.
    """

    job_id: str
    url: str
    status: str = "pending"  # pending | running | done | error
    phase: str | None = None
    error: str | None = None
    platform: str | None = None
    pages_crawled: int | None = None
    findings: list[Finding] = field(default_factory=list)
    report_markdown: str | None = None
    report_markdown_major_only: str | None = None
    is_delta: bool = False
    created_at: datetime = field(default_factory=_utcnow)


def _record_to_job(record: AuditJobRecord) -> AuditJob:
    findings = [Finding.model_validate(d) for d in json.loads(record.findings_json)] if record.findings_json else []
    return AuditJob(
        job_id=record.job_id, url=record.url, status=record.status, phase=record.phase,
        error=record.error, platform=record.platform, pages_crawled=record.pages_crawled,
        findings=findings, report_markdown=record.report_markdown,
        report_markdown_major_only=record.report_markdown_major_only, is_delta=record.is_delta,
        created_at=record.created_at,
    )


class JobStore:
    def __init__(self, db: Database, retention_days: int = 30) -> None:
        self.db = db
        self.retention_days = retention_days

    async def create(self, url: str) -> AuditJob:
        job_id = str(uuid.uuid4())
        async with self.db.session() as session:
            session.add(AuditJobRecord(job_id=job_id, url=url, status="pending"))
            await self._prune_old_jobs(session)
            await session.commit()
        return AuditJob(job_id=job_id, url=url)

    async def get(self, job_id: str) -> AuditJob | None:
        async with self.db.session() as session:
            record = await session.get(AuditJobRecord, job_id)
        return _record_to_job(record) if record is not None else None

    async def _update(self, job_id: str, **fields) -> None:
        async with self.db.session() as session:
            record = await session.get(AuditJobRecord, job_id)
            if record is None:
                logger.warning("Job %s disappeared before it could be updated - was it pruned?", job_id)
                return
            for key, value in fields.items():
                setattr(record, key, value)
            record.updated_at = _utcnow()
            await session.commit()

    async def _prune_old_jobs(self, session) -> None:
        cutoff = _utcnow() - timedelta(days=self.retention_days)
        old_ids = (await session.execute(
            select(AuditJobRecord.job_id).where(AuditJobRecord.created_at < cutoff)
        )).scalars().all()
        for old_id in old_ids:
            old_record = await session.get(AuditJobRecord, old_id)
            if old_record is not None:
                await session.delete(old_record)

    async def mark_interrupted_jobs_as_errored(self) -> int:
        """Called once at server startup: a job still "pending"/"running" in
        the DB was orphaned by whatever previously killed the process - there
        is no surviving asyncio task to resume it, so it would otherwise sit
        forever looking like it's still in progress. Returns how many rows
        were fixed up, for a clear startup log line.
        """
        async with self.db.session() as session:
            stuck = (await session.execute(
                select(AuditJobRecord).where(AuditJobRecord.status.in_(["pending", "running"]))
            )).scalars().all()
            for record in stuck:
                record.status = "error"
                record.error = "Interrupted by a server restart before this audit finished."
                record.updated_at = _utcnow()
            await session.commit()
            return len(stuck)


async def run_audit_job(job: AuditJob, settings: Settings, browser: Browser, llm_cache: LLMCache | None, store: JobStore) -> None:
    """Runs in the background (asyncio.create_task from the route handler,
    which returns job_id immediately) - the frontend polls GET /api/audits/{id}
    for progress instead of waiting on this. Every state change is written
    through `store` so a concurrent GET (or a restart) sees current state.
    """
    await store._update(job.job_id, status="running")
    try:
        await assert_public_url(job.url if "://" in job.url else f"https://{job.url}")
    except SSRFBlockedError as exc:
        await store._update(job.job_id, status="error", error=str(exc))
        return

    async def on_phase(node_name: str) -> None:
        await store._update(job.job_id, phase=node_name)
        logger.info("Job %s phase: %s", job.job_id, node_name)

    try:
        state = await run_audit_streaming(job.url, settings, browser, llm_cache, on_phase)
        findings: list[Finding] = state.get("findings", [])
        report_markdown_major_only = generate_markdown_report(state["platform"], state["site_map"], findings, major_only=True)
        await store._update(
            job.job_id,
            platform=state["platform"].platform.value,
            pages_crawled=len(state["site_map"].pages),
            findings_json=json.dumps([f.model_dump(mode="json") for f in findings]),
            report_markdown=state["report_markdown"],
            report_markdown_major_only=report_markdown_major_only,
            status="done",
        )
    except Exception as exc:  # noqa: BLE001 - a failed audit must surface as a job error, not crash the server
        logger.exception("Audit job %s failed", job.job_id)
        await store._update(job.job_id, status="error", error=str(exc))


async def run_store_rerun_job(job: AuditJob, store_id: int, service, jobs: JobStore) -> None:
    """On-demand "re-run audit now" for an already-monitored store, started
    from the Monitored Stores / Store Report screens. Tracked as an
    AuditJobRecord exactly like an ad-hoc audit (so it reuses the same
    GET /api/audits/{id} polling and report.md/report.docx download routes
    with zero new frontend rendering code) but the actual work goes through
    MonitorService.run_full_audit_streaming - the same store-monitoring
    pipeline a scheduled re-audit uses (snapshot updates, AuditRun history,
    delta-report generation), not a separate one.

    `service` is a MonitorService - not type-hinted directly to avoid a
    circular import (monitor_service.py doesn't import this module, but
    app.api.main imports both, and this keeps that import graph one-way).
    """
    await jobs._update(job.job_id, status="running")

    async def on_phase(node_name: str) -> None:
        await jobs._update(job.job_id, phase=node_name)
        logger.info("Store re-run job %s (store %d) phase: %s", job.job_id, store_id, node_name)

    try:
        result = await service.run_full_audit_streaming(store_id, trigger="manual", on_phase=on_phase)
        findings: list[Finding] = result["findings"]
        await jobs._update(
            job.job_id,
            platform=result["platform"],
            pages_crawled=result["pages_crawled"],
            findings_json=json.dumps([f.model_dump(mode="json") for f in findings]),
            report_markdown=result["report_markdown"],
            report_markdown_major_only=result["report_markdown_major_only"],
            is_delta=result["is_delta"],
            status="done",
        )
    except Exception as exc:  # noqa: BLE001 - a failed re-run must surface as a job error, not crash the server
        logger.exception("Store re-run job %s (store %d) failed", job.job_id, store_id)
        await jobs._update(job.job_id, status="error", error=str(exc))


def critical_count(findings: list[Finding]) -> int:
    return sum(1 for f in findings if f.severity == Severity.CRITICAL)


def suspension_risk_count(findings: list[Finding]) -> int:
    return sum(1 for f in findings if is_suspension_risk_finding(f))
