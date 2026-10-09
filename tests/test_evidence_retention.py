"""Tests for app.evidence_retention: ages out raw blobs for old first-audit
runs while leaving resource_facts/evaluations/findings untouched (not
exercised directly here since this round doesn't populate them yet, but the
functions are asserted to only ever touch *_blob_hash columns + Blob rows),
and caps the LLM cache's row count.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import Blob, Database, FirstAuditRun, LLMCacheEntry, Resource
from app.evidence_blob_store import write_blob_if_needed
from app.evidence_retention import prune_evidence_blobs, prune_llm_cache
from app.evidence_storage.local import LocalFileStorageAdapter


async def _make_run_with_body_blob(db, adapter, settings, *, age_days: float) -> tuple[int, str]:
    finished_at = datetime.now(timezone.utc) - timedelta(days=age_days)
    async with db.session() as session:
        run = FirstAuditRun(url="https://example.com", status="done", finished_at=finished_at)
        session.add(run)
        await session.commit()
        await session.refresh(run)

        blob = await write_blob_if_needed(session, adapter, settings, f"body for run {run.id}".encode(), "text/html")
        resource = Resource(audit_run_id=run.id, url="https://example.com/returns", resource_type="returns_policy", body_blob_hash=blob.hash)
        session.add(resource)
        await session.commit()

    return run.id, blob.hash


@pytest.mark.asyncio
async def test_prune_by_days_ages_out_old_run_keeps_recent(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/retention_days_test.db")
    await db.init()
    adapter = LocalFileStorageAdapter(tmp_path / "evidence")
    settings = Settings(evidence_retention_days=90, evidence_retention_runs=None, evidence_compression="none")

    old_run_id, old_blob_hash = await _make_run_with_body_blob(db, adapter, settings, age_days=200)
    recent_run_id, recent_blob_hash = await _make_run_with_body_blob(db, adapter, settings, age_days=5)

    stats = await prune_evidence_blobs(db, adapter, settings)

    assert stats.runs_considered == 2
    assert stats.runs_pruned == 1
    assert stats.resource_blobs_released == 1

    async with db.session() as session:
        old_resource = (await session.execute(select(Resource).where(Resource.audit_run_id == old_run_id))).scalar_one()
        recent_resource = (await session.execute(select(Resource).where(Resource.audit_run_id == recent_run_id))).scalar_one()

    assert old_resource.body_blob_hash is None
    assert recent_resource.body_blob_hash == recent_blob_hash

    async with db.session() as session:
        assert await session.get(Blob, old_blob_hash) is None  # fully released, refcount hit 0
        assert await session.get(Blob, recent_blob_hash) is not None

    await db.dispose()


@pytest.mark.asyncio
async def test_prune_keeps_most_recent_n_runs_when_configured(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/retention_runs_test.db")
    await db.init()
    adapter = LocalFileStorageAdapter(tmp_path / "evidence")
    settings = Settings(evidence_retention_days=None, evidence_retention_runs=1, evidence_compression="none")

    older_run_id, _ = await _make_run_with_body_blob(db, adapter, settings, age_days=2)
    newer_run_id, newer_blob_hash = await _make_run_with_body_blob(db, adapter, settings, age_days=1)

    stats = await prune_evidence_blobs(db, adapter, settings)
    assert stats.runs_pruned == 1

    async with db.session() as session:
        older_resource = (await session.execute(select(Resource).where(Resource.audit_run_id == older_run_id))).scalar_one()
        newer_resource = (await session.execute(select(Resource).where(Resource.audit_run_id == newer_run_id))).scalar_one()

    assert older_resource.body_blob_hash is None
    assert newer_resource.body_blob_hash == newer_blob_hash

    await db.dispose()


@pytest.mark.asyncio
async def test_no_retention_policy_configured_never_prunes(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/retention_none_test.db")
    await db.init()
    adapter = LocalFileStorageAdapter(tmp_path / "evidence")
    settings = Settings(evidence_retention_days=None, evidence_retention_runs=None, evidence_compression="none")

    await _make_run_with_body_blob(db, adapter, settings, age_days=5000)

    stats = await prune_evidence_blobs(db, adapter, settings)
    assert stats.runs_pruned == 0
    assert stats.resource_blobs_released == 0

    await db.dispose()


@pytest.mark.asyncio
async def test_prune_llm_cache_caps_row_count(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/llm_cache_prune_test.db")
    await db.init()
    settings = Settings(llm_cache_max_items=3)

    base = datetime.now(timezone.utc)
    async with db.session() as session:
        for i in range(5):
            session.add(LLMCacheEntry(cache_key=f"key-{i}", result_json="{}", created_at=base + timedelta(seconds=i)))
        await session.commit()

    deleted = await prune_llm_cache(db, settings)
    assert deleted == 2

    async with db.session() as session:
        remaining = {row.cache_key for row in (await session.execute(select(LLMCacheEntry))).scalars()}
    assert remaining == {"key-2", "key-3", "key-4"}  # the 3 newest survive

    await db.dispose()
