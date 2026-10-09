"""Tests for app.rules.engine - dispatches each ConditionType to the right
evaluator and writes Evaluation rows. Deterministic only; existing_check is
the one condition type involving an existing check function, never an LLM.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.db import Database, Evaluation, FirstAuditRun, Resource, ResourceFact
from app.models import CrawledPage, PageType, SiteMap
from app.rules.engine import evaluate_rule, run_rule_engine
from app.rules.schema import Rule


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/rules_engine_test.db")
    await database.init()
    yield database
    await database.dispose()


async def _new_run(db: Database) -> int:
    async with db.session() as session:
        run = FirstAuditRun(url="https://example.com", status="running")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run.id


async def _add_resource(db: Database, run_id: int, url: str, resource_type: str) -> int:
    async with db.session() as session:
        resource = Resource(audit_run_id=run_id, url=url, resource_type=resource_type)
        session.add(resource)
        await session.commit()
        await session.refresh(resource)
        return resource.id


async def _add_fact(db: Database, resource_id: int, fact: str, value: str, method: str = "business_identity_regex", confidence: float = 0.9) -> None:
    async with db.session() as session:
        session.add(ResourceFact(resource_id=resource_id, fact=fact, value=value, source_url="https://example.com", source_text="x", method=method, confidence=confidence))
        await session.commit()


def _missing_resource_rule() -> Rule:
    return Rule.model_validate({
        "id": "missing_returns_page", "scope": "site", "inputs": [],
        "condition": {"type": "missing_resource", "resource_type": "returns_policy"},
        "impact": "suspension_risk", "severity": "critical", "min_confidence": 1.0,
        "title": "test", "description": "test", "remediation": "test",
    })


def _all_missing_rule() -> Rule:
    return Rule.model_validate({
        "id": "missing_contact_info", "scope": "site_facts", "inputs": [],
        "condition": {"type": "all_missing", "fields": ["business.email", "business.phone"]},
        "impact": "suspension_risk", "severity": "critical", "min_confidence": 1.0,
        "title": "test", "description": "test", "remediation": "test",
    })


def _existing_check_rule() -> Rule:
    return Rule.model_validate({
        "id": "business_identity_conflict", "scope": "site_facts", "inputs": [],
        "condition": {"type": "existing_check", "check": "app.checks.business_identity.check_business_identity_consistency"},
        "impact": "suspension_risk", "severity": "critical", "min_confidence": 0.75,
        "title": "test", "description": "test", "remediation": "test",
    })


@pytest.mark.asyncio
async def test_missing_resource_fails_when_zero_reachable_pages(db):
    run_id = await _new_run(db)
    site_map = SiteMap(base_url="https://example.com", pages=[CrawledPage(url="https://example.com", page_type=PageType.HOMEPAGE, depth=0, text="hi", status=200)])

    async with db.session() as session:
        evaluations = await evaluate_rule(session, run_id, _missing_resource_rule(), site_map)
        await session.commit()

    assert len(evaluations) == 1
    assert evaluations[0].result == "fail"


@pytest.mark.asyncio
async def test_missing_resource_passes_when_genuine_page_present(db):
    run_id = await _new_run(db)
    site_map = SiteMap(base_url="https://example.com", pages=[
        CrawledPage(url="https://example.com", page_type=PageType.HOMEPAGE, depth=0, text="hi", status=200),
        CrawledPage(url="https://example.com/returns", page_type=PageType.RETURNS_POLICY, depth=1, text="Our 30 day return policy is detailed here with lots of real content.", status=200),
    ])

    async with db.session() as session:
        evaluations = await evaluate_rule(session, run_id, _missing_resource_rule(), site_map)
        await session.commit()

    assert evaluations[0].result == "pass"


@pytest.mark.asyncio
async def test_missing_resource_never_confident_when_crawl_totally_failed(db):
    run_id = await _new_run(db)
    site_map = SiteMap(base_url="https://example.com", pages=[CrawledPage(url="https://example.com", page_type=PageType.HOMEPAGE, depth=0, reachable=False, status=None)])

    async with db.session() as session:
        evaluations = await evaluate_rule(session, run_id, _missing_resource_rule(), site_map)
        await session.commit()

    assert evaluations[0].result == "needs_review"
    assert evaluations[0].confidence == 0.0


@pytest.mark.asyncio
async def test_missing_resource_is_needs_review_not_fail_when_candidate_page_was_blocked(db):
    """Confirmed live on ridge.com: a Cloudflare hard-block page was once
    read as real content (fixed at the fetch layer separately) - but even
    with that fixed, a candidate URL that was discovered and classified as
    this resource_type yet failed to fetch (blocked, network error, etc.)
    must never read as a confident "missing" CRITICAL. "Couldn't read it"
    is not "confirmed absent" - this mirrors
    app.checks.deterministic.check_required_pages' own established
    cannot_verify_matches branch, not a new invention.
    """
    run_id = await _new_run(db)
    site_map = SiteMap(base_url="https://example.com", pages=[
        CrawledPage(url="https://example.com", page_type=PageType.HOMEPAGE, depth=0, text="hi", status=200),
        CrawledPage(
            url="https://example.com/returns-policy", page_type=PageType.RETURNS_POLICY, depth=1,
            reachable=False, cannot_verify=True, status=None, failure_category="bot_blocked",
        ),
    ])

    async with db.session() as session:
        evaluations = await evaluate_rule(session, run_id, _missing_resource_rule(), site_map)
        await session.commit()

    assert len(evaluations) == 1
    assert evaluations[0].result == "needs_review"
    evidence = json.loads(evaluations[0].evidence_json)
    assert evidence["blocked_or_unreachable_pages"][0]["url"] == "https://example.com/returns-policy"
    assert evidence["blocked_or_unreachable_pages"][0]["failure_category"] == "bot_blocked"


@pytest.mark.asyncio
async def test_missing_resource_still_fails_when_genuinely_absent_not_blocked(db):
    """Don't over-correct: when there is no candidate URL for this
    resource_type at all (not even a blocked one) and the rest of the crawl
    succeeded, a confident "fail" must still fire - this is the genuine
    absence case the rule exists to catch."""
    run_id = await _new_run(db)
    site_map = SiteMap(base_url="https://example.com", pages=[
        CrawledPage(url="https://example.com", page_type=PageType.HOMEPAGE, depth=0, text="hi", status=200),
        CrawledPage(url="https://example.com/about", page_type=PageType.CONTACT_ABOUT, depth=1, text="about us", status=200),
    ])

    async with db.session() as session:
        evaluations = await evaluate_rule(session, run_id, _missing_resource_rule(), site_map)
        await session.commit()

    assert evaluations[0].result == "fail"
    assert evaluations[0].confidence == 1.0


@pytest.mark.asyncio
async def test_missing_returns_page_credited_when_off_domain_returns_portal_linked(db):
    """Confirmed live on gymshark.com: no on-domain returns page at all, but
    the homepage links to a same-organization support subdomain AND a known
    returns-portal domain - the finding must not fire; presence is credited
    instead, with the off-domain link recorded as evidence."""
    run_id = await _new_run(db)
    site_map = SiteMap(base_url="https://www.gymshark.com", pages=[
        CrawledPage(
            url="https://www.gymshark.com", page_type=PageType.HOMEPAGE, depth=0, text="hi", status=200,
            external_links=["https://support.gymshark.com/en-US/article/returns-policy", "https://us-gymshark.loopreturns.com/#/"],
        ),
    ])

    async with db.session() as session:
        evaluations = await evaluate_rule(session, run_id, _missing_resource_rule(), site_map)
        await session.commit()

    assert evaluations[0].result == "pass"
    evidence = json.loads(evaluations[0].evidence_json)
    assert "off_domain_credit" in evidence
    assert evidence["off_domain_credit"]["credited_url"]


@pytest.mark.asyncio
async def test_missing_returns_page_still_fails_when_no_off_domain_credit_either(db):
    """Don't over-correct: no on-domain page AND no off-domain link at all
    must still fire the finding - this is the genuine absence case."""
    run_id = await _new_run(db)
    site_map = SiteMap(base_url="https://example.com", pages=[
        CrawledPage(url="https://example.com", page_type=PageType.HOMEPAGE, depth=0, text="hi", status=200, external_links=["https://www.instagram.com/example"]),
    ])

    async with db.session() as session:
        evaluations = await evaluate_rule(session, run_id, _missing_resource_rule(), site_map)
        await session.commit()

    assert evaluations[0].result == "fail"


@pytest.mark.asyncio
async def test_all_missing_fails_when_no_fact_found_anywhere(db):
    run_id = await _new_run(db)
    await _add_resource(db, run_id, "https://example.com", "homepage")
    site_map = SiteMap(base_url="https://example.com", pages=[CrawledPage(url="https://example.com", page_type=PageType.HOMEPAGE, depth=0, text="hi", status=200)])

    async with db.session() as session:
        evaluations = await evaluate_rule(session, run_id, _all_missing_rule(), site_map)
        await session.commit()

    assert evaluations[0].result == "fail"


@pytest.mark.asyncio
async def test_all_missing_passes_when_any_field_found(db):
    run_id = await _new_run(db)
    resource_id = await _add_resource(db, run_id, "https://example.com", "homepage")
    await _add_fact(db, resource_id, "business.email", "support@example.com")
    site_map = SiteMap(base_url="https://example.com", pages=[CrawledPage(url="https://example.com", page_type=PageType.HOMEPAGE, depth=0, text="hi", status=200)])

    async with db.session() as session:
        evaluations = await evaluate_rule(session, run_id, _all_missing_rule(), site_map)
        await session.commit()

    assert evaluations[0].result == "pass"


@pytest.mark.asyncio
async def test_missing_contact_info_credited_when_off_domain_support_subdomain_linked(db):
    run_id = await _new_run(db)
    site_map = SiteMap(base_url="https://www.gymshark.com", pages=[
        CrawledPage(
            url="https://www.gymshark.com", page_type=PageType.HOMEPAGE, depth=0, text="hi", status=200,
            external_links=["https://support.gymshark.com/en-US"],
        ),
    ])

    async with db.session() as session:
        evaluations = await evaluate_rule(session, run_id, _all_missing_rule(), site_map)
        await session.commit()

    assert evaluations[0].result == "pass"
    evidence = json.loads(evaluations[0].evidence_json)
    assert "off_domain_credit" in evidence


@pytest.mark.asyncio
async def test_missing_contact_info_still_fails_with_no_facts_and_no_off_domain_credit(db):
    run_id = await _new_run(db)
    site_map = SiteMap(base_url="https://example.com", pages=[
        CrawledPage(url="https://example.com", page_type=PageType.HOMEPAGE, depth=0, text="hi", status=200),
    ])

    async with db.session() as session:
        evaluations = await evaluate_rule(session, run_id, _all_missing_rule(), site_map)
        await session.commit()

    assert evaluations[0].result == "fail"


@pytest.mark.asyncio
async def test_existing_check_delegates_and_maps_confidence(db):
    run_id = await _new_run(db)
    await _add_resource(db, run_id, "https://example.com/contact", "contact_about")

    site_map = SiteMap(base_url="https://example.com", pages=[
        CrawledPage(url="https://example.com/contact", page_type=PageType.CONTACT_ABOUT, depth=1, text="Email a@x.com or b@y.com for help."),
    ])

    async with db.session() as session:
        evaluations = await evaluate_rule(session, run_id, _existing_check_rule(), site_map)
        await session.commit()

    assert len(evaluations) == 1  # two different emails -> one "multiple emails" finding
    assert evaluations[0].result == "fail"
    evidence = json.loads(evaluations[0].evidence_json)
    assert evidence["delegated_finding"]["check_id"] == "business_identity_email_consistency"


@pytest.mark.asyncio
async def test_existing_check_crawl_incomplete_maps_to_needs_review(db):
    run_id = await _new_run(db)
    site_map = SiteMap(base_url="https://example.com", pages=[CrawledPage(url="https://example.com", page_type=PageType.HOMEPAGE, depth=0, reachable=False)])

    async with db.session() as session:
        evaluations = await evaluate_rule(session, run_id, _existing_check_rule(), site_map)
        await session.commit()

    assert evaluations[0].result == "needs_review"


@pytest.mark.asyncio
async def test_run_rule_engine_one_bad_rule_does_not_abort_others(db):
    run_id = await _new_run(db)
    site_map = SiteMap(base_url="https://example.com", pages=[CrawledPage(url="https://example.com", page_type=PageType.HOMEPAGE, depth=0, text="hi", status=200)])

    broken_rule = Rule.model_validate({
        "id": "missing_returns_page", "scope": "site", "inputs": [],
        "condition": {"type": "missing_resource", "resource_type": "returns_policy"},
        "impact": "suspension_risk", "severity": "critical", "min_confidence": 1.0, "title": "test", "description": "test", "remediation": "test",
    })
    broken_rule.condition.resource_type = "not_a_real_page_type"  # bypass validation, force a runtime error

    async with db.session() as session:
        evaluations = await run_rule_engine(session, run_id, [broken_rule, _all_missing_rule()], site_map)

    assert len(evaluations) == 1  # the broken rule produced nothing, but the other rule still ran
    assert evaluations[0].rule_id == "missing_contact_info"

    async with db.session() as session:
        rows = (await session.execute(select(Evaluation))).scalars().all()
    assert len(rows) == 1  # committed despite the earlier failure
