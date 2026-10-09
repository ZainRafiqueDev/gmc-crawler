"""Tests for app.first_audit.run_first_audit - the full pipeline orchestration
(discovery -> facts -> rule evaluation -> findings -> body-evidence retention
-> screenshots). detect_platform/map_site are monkeypatched (separately
tested); most tests also monkeypatch load_rules_from_yaml to an empty or
minimal rule list so the test's assertions are about THIS module's own
orchestration, not the content of the real starter rules - one test exercises
the real starter_rules.yaml end-to-end as a sanity check.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

import app.first_audit as first_audit
from app.config import Settings
from app.db import Database, FindingRecord, Resource, ResourceFact
from app.models import CrawledPage, PageType, Platform, PlatformDetectionResult, Severity, SiteMap
from app.rules.schema import Rule
from app.security.ssrf_guard import SSRFBlockedError


def _settings(tmp_path) -> Settings:
    # anthropic_api_key/openai_api_key explicitly blanked - these tests must
    # never depend on whether a real key happens to be set in this machine's
    # .env (confirmed live: without this, a real .env key made these tests
    # fire real LLM calls through app.facts.llm_commerce_facts, which then
    # hit a sqlite "database is locked" error from a second concurrent
    # session). Tests that specifically exercise the LLM-assisted path
    # monkeypatch app.facts.llm_commerce_facts.get_llm_client with a fake
    # instead - see test_facts_llm_commerce.py.
    return Settings(evidence_bucket=str(tmp_path / "evidence"), evidence_compression="none", anthropic_api_key="", openai_api_key="")


def _fake_site_map(base_url: str, pages: list[CrawledPage] | None = None) -> SiteMap:
    return SiteMap(
        base_url=base_url,
        pages=pages or [
            CrawledPage(url=base_url, page_type=PageType.HOMEPAGE, depth=0, text="hi, contact us at support@example.com", html="<html>hi</html>", status=200),
            CrawledPage(url=base_url + "/privacy", page_type=PageType.PRIVACY_POLICY, depth=1, text="privacy text", html="<html>privacy</html>", status=200),
        ],
    )


def _patch_crawl(monkeypatch, site_map: SiteMap, platform: Platform = Platform.UNKNOWN):
    async def fake_assert_public_url(url: str) -> None:
        return None

    async def fake_detect_platform(url: str) -> PlatformDetectionResult:
        return PlatformDetectionResult(platform=platform, base_url=site_map.base_url, evidence=[])

    async def fake_map_site(base_url, browser, settings, platform=None):
        return site_map

    monkeypatch.setattr(first_audit, "assert_public_url", fake_assert_public_url)
    monkeypatch.setattr(first_audit, "detect_platform", fake_detect_platform)
    monkeypatch.setattr(first_audit, "map_site", fake_map_site)


def _patch_no_rules(monkeypatch):
    monkeypatch.setattr(first_audit, "load_rules_from_yaml", lambda: [])


def _patch_rules(monkeypatch, rules: list[Rule]):
    monkeypatch.setattr(first_audit, "load_rules_from_yaml", lambda: rules)


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/first_audit_test.db")
    await database.init()
    yield database
    await database.dispose()


def test_resources_cited_in_evaluation_parses_urls_from_delegated_finding_text():
    """Confirmed live against a real store: an existing_check-delegated
    finding (e.g. "multiple phone numbers") has no single page_url (it's
    inherently multi-page) and no "observations" evidence shape - without
    scanning its own evidence text for URLs, the pages it cites never got
    marked for body retention at all."""
    from app.db import Evaluation as EvaluationModel

    resources_by_url = {
        "https://example.com/about-us": Resource(id=1, audit_run_id=1, url="https://example.com/about-us", resource_type="contact_about"),
        "https://example.com/contact-us": Resource(id=2, audit_run_id=1, url="https://example.com/contact-us", resource_type="contact_about"),
    }
    evidence = {"delegated_finding": {
        "evidence": "Found 2 distinct phone numbers: +1 (on https://example.com/about-us, https://example.com/contact-us); +44 (on https://example.com/about-us)",
        "page_url": None,
    }}
    evaluation = EvaluationModel(audit_run_id=1, rule_id="business_identity_conflict", result="fail", confidence=0.85, evidence_json=json.dumps(evidence))

    cited = first_audit._resources_cited_in_evaluation(evaluation, resources_by_url)
    assert cited == {1, 2}


@pytest.mark.asyncio
async def test_happy_path_with_no_rules_persists_resources_and_facts_and_is_compliant(db, tmp_path, monkeypatch):
    site_map = _fake_site_map("https://example.com")
    _patch_crawl(monkeypatch, site_map, Platform.WOOCOMMERCE)
    _patch_no_rules(monkeypatch)

    run_id = await first_audit.create_first_audit_run(db, "example.com")
    await first_audit.run_first_audit(run_id, "example.com", _settings(tmp_path), browser=None, db=db)

    run = await first_audit.get_first_audit_run(db, run_id)
    assert run.status == "done"
    assert run.platform == "woocommerce"
    assert run.pages_crawled == 2
    assert run.snapshot_status == "COMPLIANT"

    async with db.session() as session:
        resources = (await session.execute(select(Resource).where(Resource.audit_run_id == run_id))).scalars().all()
        resource_ids = [r.id for r in resources]
        facts = (await session.execute(select(ResourceFact).where(ResourceFact.resource_id.in_(resource_ids)))).scalars().all()
    assert len(resources) == 2
    assert any(f.fact == "business.email" and f.value == "support@example.com" for f in facts)


@pytest.mark.asyncio
async def test_ssrf_blocked_url_marks_error_without_crawling(db, tmp_path, monkeypatch):
    async def fake_assert_public_url(url: str) -> None:
        raise SSRFBlockedError("blocked: private IP")

    monkeypatch.setattr(first_audit, "assert_public_url", fake_assert_public_url)
    map_site_called = False

    async def fake_map_site(base_url, browser, settings, platform=None):
        nonlocal map_site_called
        map_site_called = True
        return _fake_site_map(base_url)

    monkeypatch.setattr(first_audit, "map_site", fake_map_site)

    run_id = await first_audit.create_first_audit_run(db, "http://169.254.169.254")
    await first_audit.run_first_audit(run_id, "http://169.254.169.254", _settings(tmp_path), browser=None, db=db)

    run = await first_audit.get_first_audit_run(db, run_id)
    assert run.status == "error"
    assert "blocked" in run.error
    assert map_site_called is False


@pytest.mark.asyncio
async def test_crawl_failure_marks_error(db, tmp_path, monkeypatch):
    async def fake_assert_public_url(url: str) -> None:
        return None

    async def fake_detect_platform(url: str) -> PlatformDetectionResult:
        return PlatformDetectionResult(platform=Platform.UNKNOWN, base_url="https://example.com", evidence=[])

    async def failing_map_site(base_url, browser, settings, platform=None):
        raise RuntimeError("playwright blew up")

    monkeypatch.setattr(first_audit, "assert_public_url", fake_assert_public_url)
    monkeypatch.setattr(first_audit, "detect_platform", fake_detect_platform)
    monkeypatch.setattr(first_audit, "map_site", failing_map_site)

    run_id = await first_audit.create_first_audit_run(db, "example.com")
    await first_audit.run_first_audit(run_id, "example.com", _settings(tmp_path), browser=None, db=db)

    run = await first_audit.get_first_audit_run(db, run_id)
    assert run.status == "error"
    assert "playwright blew up" in run.error


def _window_conflict_rule() -> Rule:
    return Rule.model_validate({
        "id": "return_window_conflict", "scope": "site_facts", "inputs": ["return.window_days"],
        "condition": {"type": "cross_page_contradiction", "fact": "return.window_days", "sources": ["returns_policy", "faq"]},
        "impact": "listing_disapproval", "severity": "high", "min_confidence": 0.5,
        "title": "Return window is stated inconsistently across pages",
        "policy_reference": "Shipping and returns policies must be accurate and consistent",
        "description": "test", "remediation": "State the same return window everywhere.",
    })


@pytest.mark.asyncio
async def test_cross_page_conflict_produces_finding_marks_cited_resources_failing_and_keeps_their_body(db, tmp_path, monkeypatch):
    site_map = _fake_site_map("https://example.com", pages=[
        CrawledPage(url="https://example.com", page_type=PageType.HOMEPAGE, depth=0, text="hi", html="<html>hi</html>", status=200),
        CrawledPage(url="https://example.com/returns", page_type=PageType.RETURNS_POLICY, depth=1, text="You may return any item within 30 days of purchase.", html="<html>returns 30</html>", status=200),
        CrawledPage(url="https://example.com/faq", page_type=PageType.FAQ, depth=1, text="Returns are accepted within 14 days only.", html="<html>faq 14</html>", status=200),
    ])
    _patch_crawl(monkeypatch, site_map)
    _patch_rules(monkeypatch, [_window_conflict_rule()])

    run_id = await first_audit.create_first_audit_run(db, "example.com")
    await first_audit.run_first_audit(run_id, "example.com", _settings(tmp_path), browser=None, db=db)

    run = await first_audit.get_first_audit_run(db, run_id)
    assert run.status == "done"
    assert run.snapshot_status == "AT_RISK"  # severity=high -> AT_RISK

    async with db.session() as session:
        findings = (await session.execute(select(FindingRecord))).scalars().all()
        assert len(findings) == 1
        finding = findings[0]
        assert finding.check_id == "return_window_conflict"
        assert finding.google_rule == "Shipping and returns policies must be accurate and consistent"
        assert finding.remediation == "State the same return window everywhere."
        assert "30" in finding.store_evidence and "14" in finding.store_evidence

        resources = {r.url: r for r in (await session.execute(select(Resource).where(Resource.audit_run_id == run_id))).scalars()}

    # Both pages cited in the contradiction's evidence kept their body;
    # the uninvolved homepage did not.
    assert resources["https://example.com/returns"].body_blob_hash is not None
    assert resources["https://example.com/faq"].body_blob_hash is not None
    assert resources["https://example.com"].body_blob_hash is None


@pytest.mark.asyncio
async def test_fail_finding_triggers_screenshot_capture_and_sets_blob_hash(db, tmp_path, monkeypatch):
    site_map = _fake_site_map("https://example.com", pages=[
        CrawledPage(url="https://example.com/returns", page_type=PageType.RETURNS_POLICY, depth=1, text="You may return any item within 30 days of purchase.", html="<html>returns 30</html>", status=200),
        CrawledPage(url="https://example.com/faq", page_type=PageType.FAQ, depth=1, text="Returns are accepted within 14 days only.", html="<html>faq 14</html>", status=200),
    ])
    _patch_crawl(monkeypatch, site_map)
    rule = _window_conflict_rule()
    rule.severity = Severity.CRITICAL
    _patch_rules(monkeypatch, [rule])

    captured_urls = []

    async def fake_capture(browser, page_url, quotes):
        captured_urls.append(page_url)
        return b"fake-webp-bytes"

    monkeypatch.setattr(first_audit, "capture_finding_screenshot_bytes", fake_capture)

    run_id = await first_audit.create_first_audit_run(db, "example.com")
    await first_audit.run_first_audit(run_id, "example.com", _settings(tmp_path), browser=None, db=db)

    assert len(captured_urls) == 1  # one screenshot attempt, for the one fail finding

    async with db.session() as session:
        finding = (await session.execute(select(FindingRecord))).scalars().one()
    assert finding.screenshot_blob_hash is not None


@pytest.mark.asyncio
async def test_needs_review_finding_does_not_trigger_screenshot_capture(db, tmp_path, monkeypatch):
    site_map = _fake_site_map("https://example.com", pages=[
        CrawledPage(url="https://example.com/returns", page_type=PageType.RETURNS_POLICY, depth=1, text="You may return any item within 30 days of purchase.", html="<html>returns 30</html>", status=200),
        CrawledPage(url="https://example.com/faq", page_type=PageType.FAQ, depth=1, text="Returns are accepted within 14 days only.", html="<html>faq 14</html>", status=200),
    ])
    _patch_crawl(monkeypatch, site_map)
    rule = _window_conflict_rule()
    rule.min_confidence = 0.95  # the real return_window_regex method confidence (0.80) falls below this -> needs_review
    _patch_rules(monkeypatch, [rule])

    captured = []

    async def fake_capture(browser, page_url, quotes):
        captured.append(page_url)
        return b"fake-webp-bytes"

    monkeypatch.setattr(first_audit, "capture_finding_screenshot_bytes", fake_capture)

    run_id = await first_audit.create_first_audit_run(db, "example.com")
    await first_audit.run_first_audit(run_id, "example.com", _settings(tmp_path), browser=None, db=db)

    assert captured == []  # needs_review findings never trigger a screenshot

    async with db.session() as session:
        finding = (await session.execute(select(FindingRecord))).scalars().one()
    assert "could not be confirmed" in finding.title
    assert finding.screenshot_blob_hash is None


@pytest.mark.asyncio
async def test_real_starter_rules_end_to_end_sanity(db, tmp_path, monkeypatch):
    """Uses the REAL starter_rules.yaml (not monkeypatched) against a small
    site with no returns page and no contact info at all - a real-world
    shape the actual starter rules should catch."""
    site_map = _fake_site_map("https://example.com", pages=[
        CrawledPage(url="https://example.com", page_type=PageType.HOMEPAGE, depth=0, text="Welcome to our store.", html="<html></html>", status=200),
    ])
    _patch_crawl(monkeypatch, site_map)

    run_id = await first_audit.create_first_audit_run(db, "example.com")
    await first_audit.run_first_audit(run_id, "example.com", _settings(tmp_path), browser=None, db=db)

    run = await first_audit.get_first_audit_run(db, run_id)
    assert run.status == "done"
    assert run.snapshot_status == "CRITICAL"  # missing returns page + missing contact info, both severity=critical

    async with db.session() as session:
        findings = (await session.execute(select(FindingRecord))).scalars().all()
    check_ids = {f.check_id for f in findings}
    assert "missing_returns_page" in check_ids
    assert "missing_contact_info" in check_ids
    for finding in findings:
        assert finding.google_rule  # every finding has a Google rule
        assert finding.remediation  # ...and an exact fix
        assert finding.consequence  # ...and a stated risk/consequence
