"""Retention/prune job (spec: "make it a config, not hardcoded"). Ages out
Tier 2 raw-evidence blobs for old FirstAuditRuns, and caps the LLM result
cache's row count - both are callable directly (tests, a one-off CLI
invocation) and scheduleable via app.scheduling.SchedulerBackend, the same
way app.policy_watcher's job already is.

What this NEVER touches: resource_facts, evaluations, findings, or any
status-history field. Only Resource.body_blob_hash/jsonld_blob_hash,
FindingRecord.screenshot_blob_hash, and the underlying Blob rows/objects they
reference - exactly the "collapse to hash + status + facts only, delete the
raw blobs" behavior from the spec, never the structured record itself.

Retention has two independent, optional axes - evidence_retention_days and
evidence_retention_runs. A run's evidence is kept if it satisfies EITHER
configured axis ("the last N runs OR 90 days", read as alternative ways to
qualify as recent); if neither is configured, nothing is ever pruned. Note:
runs aren't currently grouped by store (first audits are standalone this
round - see app.first_audit), so evidence_retention_runs ranks across ALL
runs globally, not per-store; that becomes per-store ranking once a future
round links a FirstAuditRun to a MonitoredStore.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import Database, FindingRecord, FirstAuditRun, LLMCacheEntry, Resource
from app.evidence_blob_store import release_blob
from app.evidence_storage.base import StorageAdapter

logger = logging.getLogger("gmc_audit.evidence_retention")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class PruneStats:
    runs_considered: int = 0
    runs_pruned: int = 0
    resource_blobs_released: int = 0
    finding_blobs_released: int = 0
    llm_cache_rows_deleted: int = 0


async def _runs_to_prune(session: AsyncSession, settings: Settings) -> list[FirstAuditRun]:
    all_runs = list((await session.execute(
        select(FirstAuditRun).order_by(FirstAuditRun.started_at.desc())
    )).scalars())

    if settings.evidence_retention_days is None and settings.evidence_retention_runs is None:
        return []  # no policy configured - never prune

    now = _utcnow()
    to_prune: list[FirstAuditRun] = []
    for rank, run in enumerate(all_runs):
        reference_time = run.finished_at or run.started_at
        if reference_time.tzinfo is None:
            reference_time = reference_time.replace(tzinfo=timezone.utc)
        age_days = (now - reference_time) / timedelta(days=1)

        keep = False
        if settings.evidence_retention_days is not None and age_days <= settings.evidence_retention_days:
            keep = True
        if settings.evidence_retention_runs is not None and rank < settings.evidence_retention_runs:
            keep = True
        if not keep:
            to_prune.append(run)

    return to_prune


async def prune_evidence_blobs(db: Database, adapter: StorageAdapter, settings: Settings) -> PruneStats:
    stats = PruneStats()
    async with db.session() as session:
        all_count = (await session.execute(select(FirstAuditRun.id))).scalars().all()
        stats.runs_considered = len(all_count)

        runs = await _runs_to_prune(session, settings)
        stats.runs_pruned = len(runs)

        for run in runs:
            resources = (await session.execute(
                select(Resource).where(Resource.audit_run_id == run.id)
            )).scalars().all()
            for resource in resources:
                if resource.body_blob_hash is not None:
                    await release_blob(session, adapter, resource.body_blob_hash)
                    resource.body_blob_hash = None
                    stats.resource_blobs_released += 1
                if resource.jsonld_blob_hash is not None:
                    await release_blob(session, adapter, resource.jsonld_blob_hash)
                    resource.jsonld_blob_hash = None
                    stats.resource_blobs_released += 1

            findings = (await session.execute(
                select(FindingRecord).where(FindingRecord.audit_run_id == run.id)
            )).scalars().all()
            for finding in findings:
                if finding.screenshot_blob_hash is not None:
                    await release_blob(session, adapter, finding.screenshot_blob_hash)
                    finding.screenshot_blob_hash = None
                    stats.finding_blobs_released += 1

        await session.commit()

    logger.info(
        "Evidence prune: %d/%d run(s) aged out, %d resource blob(s) + %d finding blob(s) released",
        stats.runs_pruned, stats.runs_considered, stats.resource_blobs_released, stats.finding_blobs_released,
    )
    return stats


async def prune_llm_cache(db: Database, settings: Settings) -> int:
    """Hard cap on LLMCacheEntry row count (Tier 3: the cache must be
    bounded). Keeps the newest `llm_cache_max_items` rows, deletes the rest -
    independent of, and in addition to, LLMCache.get's existing max_age_days
    staleness check (which skips stale rows on read but never deletes them).
    """
    async with db.session() as session:
        ids_to_keep = (await session.execute(
            select(LLMCacheEntry.id).order_by(LLMCacheEntry.created_at.desc()).limit(settings.llm_cache_max_items)
        )).scalars().all()

        all_ids = (await session.execute(select(LLMCacheEntry.id))).scalars().all()
        to_delete = set(all_ids) - set(ids_to_keep)

        for entry_id in to_delete:
            entry = await session.get(LLMCacheEntry, entry_id)
            if entry is not None:
                await session.delete(entry)

        await session.commit()

    if to_delete:
        logger.info("LLM cache prune: deleted %d row(s) beyond llm_cache_max_items=%d", len(to_delete), settings.llm_cache_max_items)
    return len(to_delete)


async def run_retention_job(db: Database, adapter: StorageAdapter, settings: Settings) -> None:
    """Single entry point for app.scheduling.SchedulerBackend.add_interval_job
    - runs both prune passes. Exceptions are logged, not raised: a failed
    prune pass must never crash the scheduler loop or any caller awaiting it
    fire-and-forget, the same discipline as app.policy_watcher's job.
    """
    try:
        await prune_evidence_blobs(db, adapter, settings)
    except Exception:
        logger.exception("Evidence blob prune pass failed")

    try:
        await prune_llm_cache(db, settings)
    except Exception:
        logger.exception("LLM cache prune pass failed")
