"""Standalone "First Audit" pipeline (GMC bot follow-up spec) - the full
work-order: discovery + classification (step 3) -> fact extraction (step 4
piece 1/2) -> contradiction/rule evaluation (piece 3/4) -> findings +
compliance snapshot (piece 4) -> PASS-drops-body evidence retention, with
screenshots for FAIL findings (piece 4, steps 4-5).

Deliberately reuses existing, separately-tested stages rather than
rebuilding them: app.platform_detector.detect_platform, app.site_mapper.map_site
(discovery/classification), app.facts.extractor (fact extraction),
app.contradiction_engine + app.rules.engine (rule evaluation),
app.findings_builder (Evaluation -> FindingRecord + snapshot status),
app.evidence_store.store_resource_body_evidence (Tier 2 retention),
app.checks.screenshot_annotator.capture_finding_screenshot_bytes (evidence
screenshots). This module is the orchestration, not the logic.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from playwright.async_api import Browser

from app.checks.screenshot_annotator import _candidate_quotes, capture_finding_screenshot_bytes
from app.config import Settings
from app.db import Database, Evaluation, FindingRecord, FirstAuditRun, Resource
from app.evidence_blob_store import write_blob_if_needed
from app.evidence_store import persist_facts, persist_resources, store_resource_body_evidence
from app.evidence_storage import get_storage_adapter
from app.facts.extractor import extract_all_facts
from app.findings_builder import build_findings_for_run, compute_snapshot_status
from app.llm.cache import LLMCache
from app.models import CrawledPage, SiteMap
from app.platform_detector import detect_platform
from app.rules.engine import run_rule_engine
from app.rules.loader import load_rules_from_yaml, sync_rules_to_db
from app.rules.schema import Rule
from app.security.ssrf_guard import SSRFBlockedError, assert_public_url
from app.site_mapper import map_site

logger = logging.getLogger("gmc_audit.first_audit")

_SCREENSHOT_WORTHY_SEVERITIES = {"critical", "high"}
_URL_RE = re.compile(r"https?://[^\s,;()]+")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def create_first_audit_run(db: Database, url: str) -> int:
    async with db.session() as session:
        run = FirstAuditRun(url=url, status="pending")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run.id


async def get_first_audit_run(db: Database, run_id: int) -> FirstAuditRun | None:
    async with db.session() as session:
        return await session.get(FirstAuditRun, run_id)


async def _mark_error(db: Database, run_id: int, error: str) -> None:
    async with db.session() as session:
        run = await session.get(FirstAuditRun, run_id)
        run.status = "error"
        run.error = error
        run.finished_at = _utcnow()
        await session.commit()


def _resources_cited_in_evaluation(evaluation: Evaluation, resources_by_url: dict[str, Resource]) -> set[int]:
    """A site-wide (resource_id=None) evaluation still cites specific pages
    as evidence - either structurally (evidence_json["observations"][*]
    ["source_url"], from the contradiction engine) or only as plain text
    inside a delegated existing_check Finding's own evidence string (e.g.
    "Found 2 distinct phone numbers: ... (on https://.../about-us,
    https://.../contact-us)") - confirmed live against a real store: an
    existing_check finding has no single page_url (it's inherently multi-
    page), so without this text-scan fallback its cited pages never got
    marked for body retention at all. Either way, a page cited as evidence
    is exactly as defensible to keep as a per-resource failure's own page.
    """
    evidence = json.loads(evaluation.evidence_json)
    cited: set[int] = set()

    for obs in evidence.get("observations", []):
        resource = resources_by_url.get(obs.get("source_url"))
        if resource is not None:
            cited.add(resource.id)

    delegated = evidence.get("delegated_finding")
    if delegated:
        text = f"{delegated.get('evidence', '')} {delegated.get('page_url', '')}"
        for raw_url in _URL_RE.findall(text):
            resource = resources_by_url.get(raw_url.rstrip(".,;:)"))
            if resource is not None:
                cited.add(resource.id)

    return cited


def _failing_resource_ids(evaluations: list[Evaluation], resources_by_url: dict[str, Resource]) -> set[int]:
    failing: set[int] = set()
    for evaluation in evaluations:
        if evaluation.result == "pass":
            continue
        if evaluation.resource_id is not None:
            failing.add(evaluation.resource_id)
        else:
            failing |= _resources_cited_in_evaluation(evaluation, resources_by_url)
    return failing


async def _apply_body_evidence_retention(
    session, adapter, settings: Settings, resources: list[Resource], pages_by_url: dict[str, CrawledPage],
    evaluations: list[Evaluation], resources_by_url: dict[str, Resource],
) -> None:
    """PASS-drops-body (work-order step 4, piece 4 step 4): now that rule
    evaluation is done and every resource's pass/fail status is known, decide
    per-resource whether its raw body earns Tier 2 retention.

    `resources` were loaded in an earlier (now-closed) session - each one is
    re-fetched by id in THIS session before being mutated, since a detached
    object's attribute changes are never tracked by a different session's
    unit-of-work (confirmed live: without this, body_blob_hash silently
    never persisted - the mutation happened on an orphaned Python object).
    """
    failing_ids = _failing_resource_ids(evaluations, resources_by_url)
    for stale_resource in resources:
        page = pages_by_url.get(stale_resource.url)
        if page is None:
            continue
        resource = await session.get(Resource, stale_resource.id)
        await store_resource_body_evidence(session, adapter, settings, resource, page, resource.id in failing_ids)
    await session.commit()


async def _capture_finding_screenshots(
    session, adapter, settings: Settings, browser: Browser, findings: list[FindingRecord], evaluations_by_id: dict[int, Evaluation],
) -> None:
    """Work-order step 4, piece 4 step 5: the FAIL path starts capturing
    screenshots here. Only FAIL (not needs_review) findings, and only
    critical/high severity when settings.screenshots_only_critical is set
    (the default) - a second live-browser visit per finding is expensive and
    only worth it as defensible evidence for a genuinely serious result.

    `findings` were loaded in an earlier (now-closed) session - each one is
    re-fetched by id in THIS session before screenshot_blob_hash is set, for
    the same reason _apply_body_evidence_retention re-fetches resources.
    """
    for stale_finding in findings:
        if stale_finding.page_url is None:
            continue
        evaluation = evaluations_by_id.get(stale_finding.evaluation_id)
        if evaluation is None or evaluation.result != "fail":
            continue
        if settings.screenshots_only_critical and stale_finding.severity not in _SCREENSHOT_WORTHY_SEVERITIES:
            continue

        quotes = _candidate_quotes(stale_finding.store_evidence)
        try:
            screenshot_bytes = await capture_finding_screenshot_bytes(browser, stale_finding.page_url, quotes)
        except Exception:
            logger.exception("Screenshot capture failed for finding %s on %s - continuing without it", stale_finding.check_id, stale_finding.page_url)
            continue
        if screenshot_bytes is None:
            continue

        blob = await write_blob_if_needed(session, adapter, settings, screenshot_bytes, "image/webp")
        finding = await session.get(FindingRecord, stale_finding.id)
        finding.screenshot_blob_hash = blob.hash

    await session.commit()


async def run_first_audit(run_id: int, url: str, settings: Settings, browser: Browser, db: Database) -> None:
    """Runs the full First Audit pipeline for one FirstAuditRun, in the
    background (same fire-and-forget-task pattern as app.api.jobs.run_audit_job
    - caller asyncio.create_task's this and polls FirstAuditRun.status/
    snapshot_status instead of awaiting it directly).
    """
    async with db.session() as session:
        run = await session.get(FirstAuditRun, run_id)
        run.status = "running"
        await session.commit()

    try:
        await assert_public_url(url if "://" in url else f"https://{url}")
    except SSRFBlockedError as exc:
        await _mark_error(db, run_id, str(exc))
        return

    try:
        platform_result = await detect_platform(url)
        site_map: SiteMap = await map_site(platform_result.base_url, browser, settings, platform=platform_result.platform)
        pages_by_url = {p.url: p for p in site_map.pages}

        llm_cache = LLMCache(db)
        async with db.session() as session:
            resources = await persist_resources(session, run_id, site_map)
            resources_by_url = {r.url: r for r in resources}
            facts = await extract_all_facts(site_map, settings, llm_cache)
            await persist_facts(session, resources_by_url, facts)

            run = await session.get(FirstAuditRun, run_id)
            run.platform = platform_result.platform.value
            run.pages_crawled = len(site_map.pages)
            run.unreachable_pages_count = sum(1 for p in site_map.pages if p.cannot_verify)
            await session.commit()

        rules: list[Rule] = load_rules_from_yaml()
        rules_by_id = {r.id: r for r in rules}
        async with db.session() as session:
            await sync_rules_to_db(session, rules)

        async with db.session() as session:
            evaluations = await run_rule_engine(session, run_id, rules, site_map)

        async with db.session() as session:
            findings = await build_findings_for_run(session, rules_by_id, evaluations)
            await session.commit()

        adapter = get_storage_adapter(settings)
        async with db.session() as session:
            await _apply_body_evidence_retention(session, adapter, settings, resources, pages_by_url, evaluations, resources_by_url)

        evaluations_by_id = {e.id: e for e in evaluations}
        async with db.session() as session:
            await _capture_finding_screenshots(session, adapter, settings, browser, findings, evaluations_by_id)

        async with db.session() as session:
            run = await session.get(FirstAuditRun, run_id)
            run.snapshot_status = compute_snapshot_status(findings)
            run.status = "done"
            run.finished_at = _utcnow()
            await session.commit()

        logger.info(
            "First audit %d complete: %d page(s), %d fact(s), %d evaluation(s), %d finding(s), status=%s (%s)",
            run_id, len(site_map.pages), len(facts), len(evaluations), len(findings), run.snapshot_status, platform_result.platform.value,
        )
    except Exception as exc:
        logger.exception("First audit %d failed", run_id)
        await _mark_error(db, run_id, str(exc))
