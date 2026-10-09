"""Persists a crawled SiteMap into the First Audit evidence store (spec
section 2.7: "persist every resource - http_status, content_hash,
schema_hash, clean text, JSON-LD, extracted facts, rule results,
timestamps").

Deliberately thin: SiteMap/CrawledPage (app.models) stay the in-memory shape
the existing crawl/classify/deterministic/LLM pipeline already works with -
reused as-is, not replaced. This module is the one place that turns that
in-memory shape into the durable app.db.Resource rows an admin UI and future
runs can query. schema_hash/clean-text/JSON-LD/facts/rule-results come from
work-order step 4 (fact extraction + JSON-LD + rule engine) - this round only
writes what discovery + classification already produce.

persist_resources (discovery time) never writes a raw-body blob - only
store_resource_body_evidence (called once rule evaluation is known, see its
own docstring) decides whether a resource's body earns Tier 2 storage at all.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.change_detection import compute_content_hash
from app.config import Settings
from app.db import Resource, ResourceFact
from app.evidence_blob_store import release_blob, write_blob_if_needed
from app.evidence_storage.base import StorageAdapter
from app.facts.types import FactRecord
from app.models import CrawledPage, SiteMap


def _discovered_via(is_homepage: bool) -> str:
    # Best-effort only - see Resource's docstring in app/db.py for why this
    # isn't a precise sitemap/nav/footer distinction.
    return "homepage" if is_homepage else "crawl"


async def persist_resources(session: AsyncSession, audit_run_id: int, site_map: SiteMap) -> list[Resource]:
    """Writes one Resource row per page in site_map.pages. Caller commits -
    this function only adds to the session, so it composes with whatever
    else a caller writes in the same transaction (e.g. the FirstAuditRun
    status update)."""
    resources: list[Resource] = []
    for page in site_map.pages:
        resource = Resource(
            audit_run_id=audit_run_id,
            url=page.url,
            resource_type=page.page_type.value,
            http_status=page.status,
            content_hash=compute_content_hash(page.text) if page.reachable else None,
            discovered_via=_discovered_via(page.url == site_map.base_url),
        )
        session.add(resource)
        resources.append(resource)

    await session.flush()  # assign ids without ending the caller's transaction
    return resources


async def persist_facts(session: AsyncSession, resources_by_url: dict[str, Resource], facts: list[FactRecord]) -> list[ResourceFact]:
    """Writes one ResourceFact row per extracted FactRecord (app.facts.extractor,
    piece 1/2), linked to the Resource row for the page the fact came from.
    A fact whose resource_url isn't in resources_by_url is skipped, not
    errored - persist_resources runs first in the same audit and should
    always cover every page a fact extractor looked at, but this stays
    defensive rather than crashing a whole audit over one mismatched URL
    (e.g. a trailing-slash/redirect difference between the two passes).
    """
    rows: list[ResourceFact] = []
    for fact in facts:
        resource = resources_by_url.get(fact.resource_url)
        if resource is None:
            continue
        row = ResourceFact(
            resource_id=resource.id, fact=fact.fact, value=fact.value,
            source_url=fact.source_url, source_text=fact.source_text,
            method=fact.method, confidence=fact.confidence, variant_key=fact.variant_key,
        )
        session.add(row)
        rows.append(row)

    await session.flush()
    return rows


async def store_resource_body_evidence(
    session: AsyncSession, adapter: StorageAdapter, settings: Settings,
    resource: Resource, page: CrawledPage, has_failing_evaluation: bool, jsonld_raw: str | None = None,
) -> None:
    """Tier 2 write path, called once rule-evaluation results are known for
    this resource (work-order step 4's rule engine calls this per resource -
    NOT called from persist_resources above, which only ever runs at
    discovery time, before any rule has evaluated anything).

    "PASS drops body" (spec section 2.2): a resource with no failing/needs-
    review evaluation keeps only what persist_resources already wrote
    (content_hash + resource_type + status - all small, Tier 1) unless
    settings.keep_body_on_pass overrides this. Only a failing/needs-review
    resource's raw HTML (and JSON-LD, if any was extracted) is uploaded to
    Tier 2 and referenced from Resource.body_blob_hash/jsonld_blob_hash.

    If this resource previously had a body blob stored (e.g. a re-audit that
    now passes where it used to fail), that blob's reference is released
    instead of silently orphaning it.
    """
    should_keep_body = has_failing_evaluation or settings.keep_body_on_pass

    if not should_keep_body:
        if resource.body_blob_hash is not None:
            await release_blob(session, adapter, resource.body_blob_hash)
            resource.body_blob_hash = None
        if resource.jsonld_blob_hash is not None:
            await release_blob(session, adapter, resource.jsonld_blob_hash)
            resource.jsonld_blob_hash = None
        return

    if page.html:
        blob = await write_blob_if_needed(session, adapter, settings, page.html.encode("utf-8"), "text/html")
        resource.body_blob_hash = blob.hash

    if jsonld_raw:
        blob = await write_blob_if_needed(session, adapter, settings, jsonld_raw.encode("utf-8"), "application/ld+json")
        resource.jsonld_blob_hash = blob.hash
