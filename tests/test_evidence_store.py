"""Tests for app.evidence_store.persist_resources: a SiteMap's pages become
Resource rows with the right resource_type, content_hash (only for reachable
pages), and discovered_via.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.change_detection import compute_content_hash
from app.config import Settings
from app.db import Blob, Database, FirstAuditRun, Resource, ResourceFact
from app.evidence_store import persist_facts, persist_resources, store_resource_body_evidence
from app.evidence_storage.local import LocalFileStorageAdapter
from app.facts.types import make_fact
from app.models import CrawledPage, PageType, SiteMap


def _site_map() -> SiteMap:
    return SiteMap(
        base_url="https://example.com",
        pages=[
            CrawledPage(url="https://example.com", page_type=PageType.HOMEPAGE, depth=0, text="Welcome home"),
            CrawledPage(url="https://example.com/returns", page_type=PageType.RETURNS_POLICY, depth=1, text="30 day returns", status=200),
            CrawledPage(url="https://example.com/gone", page_type=PageType.BLOG_OTHER, depth=1, reachable=False, status=404, error="not_found"),
        ],
    )


@pytest.mark.asyncio
async def test_persist_resources_writes_one_row_per_page(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/evidence_test.db")
    await db.init()

    site_map = _site_map()
    async with db.session() as session:
        run = FirstAuditRun(url=site_map.base_url, status="running")
        session.add(run)
        await session.commit()
        await session.refresh(run)

        resources = await persist_resources(session, run.id, site_map)
        await session.commit()

    assert len(resources) == 3

    async with db.session() as session:
        rows = {r.url: r for r in (await session.execute(select(Resource))).scalars()}

    assert rows["https://example.com"].resource_type == "homepage"
    assert rows["https://example.com"].discovered_via == "homepage"
    assert rows["https://example.com"].content_hash == compute_content_hash("Welcome home")

    assert rows["https://example.com/returns"].resource_type == "returns_policy"
    assert rows["https://example.com/returns"].discovered_via == "crawl"
    assert rows["https://example.com/returns"].http_status == 200

    # Unreachable page: no content hash fabricated from empty/missing text.
    assert rows["https://example.com/gone"].content_hash is None
    assert rows["https://example.com/gone"].http_status == 404

    await db.dispose()


@pytest.mark.asyncio
async def test_persist_facts_links_to_resource_and_skips_unknown_url(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/persist_facts_test.db")
    await db.init()

    site_map = _site_map()
    async with db.session() as session:
        run = FirstAuditRun(url=site_map.base_url, status="running")
        session.add(run)
        await session.commit()
        await session.refresh(run)

        resources = await persist_resources(session, run.id, site_map)
        await session.commit()
        resources_by_url = {r.url: r for r in resources}

        facts = [
            make_fact("https://example.com/returns", "return.window_days", "30", "https://example.com/returns", "within 30 days", "return_window_regex"),
            make_fact("https://no-such-resource.example.com", "business.email", "x@y.com", "https://no-such-resource.example.com", "x@y.com", "business_identity_regex"),
        ]
        rows = await persist_facts(session, resources_by_url, facts)
        await session.commit()

    assert len(rows) == 1  # the unmatched-URL fact is skipped, not errored

    async with db.session() as session:
        stored = (await session.execute(select(ResourceFact))).scalars().all()
    assert len(stored) == 1
    assert stored[0].fact == "return.window_days"
    assert stored[0].value == "30"
    assert stored[0].confidence == pytest.approx(0.80)

    await db.dispose()


@pytest.mark.asyncio
async def test_store_resource_body_evidence_pass_drops_body_fail_keeps_it(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/body_evidence_test.db")
    await db.init()
    adapter = LocalFileStorageAdapter(tmp_path / "evidence")
    settings = Settings(keep_body_on_pass=False, evidence_compression="none")

    passing_page = CrawledPage(url="https://example.com/a", page_type=PageType.RETURNS_POLICY, depth=1, html="<html>a</html>", status=200)
    failing_page = CrawledPage(url="https://example.com/b", page_type=PageType.SHIPPING_POLICY, depth=1, html="<html>b</html>", status=200)

    async with db.session() as session:
        run = FirstAuditRun(url="https://example.com", status="running")
        session.add(run)
        await session.commit()
        await session.refresh(run)

        [passing_resource, failing_resource] = await persist_resources(session, run.id, SiteMap(base_url="https://example.com", pages=[passing_page, failing_page]))
        await session.commit()

        await store_resource_body_evidence(session, adapter, settings, passing_resource, passing_page, has_failing_evaluation=False)
        await store_resource_body_evidence(session, adapter, settings, failing_resource, failing_page, has_failing_evaluation=True)
        await session.commit()

        passing_id, failing_id = passing_resource.id, failing_resource.id

    async with db.session() as session:
        passing_resource = await session.get(Resource, passing_id)
        failing_resource = await session.get(Resource, failing_id)

    assert passing_resource.body_blob_hash is None  # PASS: no body uploaded
    assert failing_resource.body_blob_hash is not None  # FAIL: body kept as evidence
    assert adapter.get(failing_resource.body_blob_hash) == b"<html>b</html>"

    await db.dispose()


@pytest.mark.asyncio
async def test_store_resource_body_evidence_releases_blob_when_resource_starts_passing(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/body_evidence_release_test.db")
    await db.init()
    adapter = LocalFileStorageAdapter(tmp_path / "evidence")
    settings = Settings(keep_body_on_pass=False, evidence_compression="none")

    page = CrawledPage(url="https://example.com/a", page_type=PageType.RETURNS_POLICY, depth=1, html="<html>a</html>", status=200)

    async with db.session() as session:
        run = FirstAuditRun(url="https://example.com", status="running")
        session.add(run)
        await session.commit()
        await session.refresh(run)

        [resource] = await persist_resources(session, run.id, SiteMap(base_url="https://example.com", pages=[page]))
        await session.commit()

        # First run: this resource fails -> body kept.
        await store_resource_body_evidence(session, adapter, settings, resource, page, has_failing_evaluation=True)
        await session.commit()
        blob_hash = resource.body_blob_hash
        resource_id = resource.id

    assert blob_hash is not None

    async with db.session() as session:
        resource = await session.get(Resource, resource_id)
        # Re-audit: now passes -> body must be released, not left orphaned.
        await store_resource_body_evidence(session, adapter, settings, resource, page, has_failing_evaluation=False)
        await session.commit()

    async with db.session() as session:
        resource = await session.get(Resource, resource_id)
        assert resource.body_blob_hash is None
        assert await session.get(Blob, blob_hash) is None

    await db.dispose()
