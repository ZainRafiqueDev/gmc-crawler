"""Rule-engine evaluation (work-order step 4, piece 4, part 2): evaluates
every active rule against a FirstAuditRun's facts/evaluations, producing
app.db.Evaluation rows. Deterministic rules decide results here - nothing in
this module calls an LLM; the one condition type that touches "semantic
judgement" (existing_check) delegates entirely to an already-existing,
already-tested Python check function, never to a fresh LLM call.

Dispatch by ConditionType, one evaluator per type:
- cross_page_contradiction / numeric_mismatch -> app.contradiction_engine
  (piece 3, unchanged - this module only supplies the rule's own parameters).
- missing_resource -> reuses app.checks.deterministic's own soft-404-aware
  "genuine vs flagged vs truly absent" split (_split_genuine_from_soft_404)
  and SiteMap.crawl_totally_failed guard - the exact same false-positive
  protections that check_required_pages already relies on, not reimplemented.
  For resource_type=returns_policy specifically, also checks
  app.off_domain_credit.find_returns_credit before concluding absence -
  confirmed live (gymshark.com) that a large, well-run store's returns page
  can legitimately live off-domain (a subdomain, or a known third-party
  returns portal); "we didn't crawl it" must not be scored as "it isn't
  there" any more than a blocked page should be.
- all_missing -> a bounded existence query over ResourceFact (never a full
  table scan), also guarded by crawl_totally_failed (a failed crawl finding
  zero facts is not evidence the facts don't exist on the real site). For
  the business.email/business.phone field set specifically, also checks
  app.off_domain_credit.find_contact_credit - same off-domain principle.
- existing_check -> dynamically imports and calls the named function
  (currently only app.checks.business_identity.check_business_identity_consistency),
  converting each Finding it returns into an Evaluation that preserves the
  original finding verbatim (evidence_json["delegated_finding"]) so piece 4's
  finding-emission step never has to re-derive what that check already said.
"""
from __future__ import annotations

import importlib
import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.checks.deterministic import _split_genuine_from_soft_404
from app.contradiction_engine import evaluate_cross_page_contradiction, evaluate_per_resource_method_comparison
from app.db import Evaluation, Resource, ResourceFact
from app.models import Confidence, PageType, SiteMap
from app.off_domain_credit import find_contact_credit, find_returns_credit
from app.rules.schema import ConditionType, Rule

logger = logging.getLogger("gmc_audit.rules.engine")

# How a delegated existing_check Finding's own Confidence maps onto this
# round's (result, confidence) shape - CONFIRMED/POTENTIAL_RISK are both real,
# actionable findings ("fail"); CANNOT_VERIFY is the same CANNOT_VERIFY-style
# downgrade every other condition type uses ("needs_review"). The confidence
# float itself is only ever used for this project's own needs_review-gating
# logic elsewhere, never shown to a reader, so a representative fixed value
# per bucket (rather than inventing a finer scale the source check never
# expressed) is sufficient.
_DELEGATED_CONFIDENCE_BY_RESULT: dict[Confidence, tuple[str, float]] = {
    Confidence.CONFIRMED: ("fail", 1.0),
    Confidence.POTENTIAL_RISK: ("fail", 0.85),
    Confidence.CANNOT_VERIFY: ("needs_review", 0.3),
}


async def _evaluate_missing_resource(session: AsyncSession, audit_run_id: int, rule: Rule, site_map: SiteMap) -> Evaluation | None:
    """Core principle (confirmed live, a real false positive on ridge.com -
    a bot-protection block page was briefly read as real content before
    app.fetch's hard-block detection existed): "couldn't read it" must never
    be scored as "it isn't there." A candidate URL that was discovered and
    classified as this resource_type but whose fetch failed (blocked,
    network error, etc. - CrawledPage.cannot_verify, mirroring
    app.checks.deterministic.check_required_pages' own established
    cannot_verify_matches branch, reused here rather than reimplemented)
    must downgrade to needs_review, never a confident "fail" - the same
    priority order that check already uses: genuine > soft-404-ambiguous >
    fetch-failed-candidate > confirmed absent.
    """
    if site_map.crawl_totally_failed:
        evidence = {"reason": "crawl_totally_failed", "resource_type": rule.condition.resource_type}
        result, confidence = "needs_review", 0.0
    else:
        page_type = PageType(rule.condition.resource_type)
        matches = site_map.pages_of_type(page_type)
        reachable = [p for p in matches if p.reachable]
        genuine, soft_404_flagged = _split_genuine_from_soft_404(reachable, site_map)
        cannot_verify_matches = [p for p in matches if p.cannot_verify]

        if genuine:
            result, confidence = "pass", 1.0
            evidence = {"resource_type": page_type.value, "genuine_pages": [p.url for p in genuine]}
        elif soft_404_flagged:
            result, confidence = "needs_review", 0.5
            evidence = {"resource_type": page_type.value, "soft_404_flagged_pages": [p.url for p, _ in soft_404_flagged]}
        elif cannot_verify_matches:
            result, confidence = "needs_review", 0.3
            evidence = {
                "resource_type": page_type.value,
                "blocked_or_unreachable_pages": [
                    {"url": p.url, "failure_category": p.failure_category} for p in cannot_verify_matches
                ],
            }
        elif page_type == PageType.RETURNS_POLICY and (credit := find_returns_credit(site_map)) is not None:
            # Detect-and-credit (confirmed live, gymshark.com): no on-domain
            # returns page, but a same-organization subdomain or known
            # returns-portal is linked - presence is credited, content is
            # NOT audited (explicit in the evidence, never a silent pass).
            # Only wired for returns_policy - no other resource_type has an
            # analogous well-known off-domain pattern this round.
            result, confidence = "pass", 0.8
            evidence = {"resource_type": page_type.value, "off_domain_credit": credit}
        else:
            result, confidence = "fail", 1.0
            evidence = {"resource_type": page_type.value, "reachable_count": 0}

    evaluation = Evaluation(audit_run_id=audit_run_id, rule_id=rule.id, resource_id=None, result=result, confidence=confidence, evidence_json=json.dumps(evidence))
    session.add(evaluation)
    await session.flush()
    return evaluation


async def _evaluate_all_missing(session: AsyncSession, audit_run_id: int, rule: Rule, site_map: SiteMap) -> Evaluation | None:
    if site_map.crawl_totally_failed:
        evidence = {"reason": "crawl_totally_failed", "fields": rule.condition.fields}
        result, confidence = "needs_review", 0.0
    else:
        stmt = (
            select(ResourceFact)
            .join(Resource, ResourceFact.resource_id == Resource.id)
            .where(Resource.audit_run_id == audit_run_id, ResourceFact.fact.in_(rule.condition.fields))
            .limit(1)
        )
        found = (await session.execute(stmt)).scalars().first()
        if found is not None:
            result, confidence = "pass", 1.0
            evidence = {"found_fact": found.fact, "value": found.value}
        elif set(rule.condition.fields) == {"business.email", "business.phone"} and (credit := find_contact_credit(site_map)) is not None:
            # Detect-and-credit, same principle as the returns case above -
            # only wired for exactly this named contact-info field set, not
            # a generic hook for every possible all_missing rule.
            result, confidence = "pass", 0.8
            evidence = {"fields_checked": rule.condition.fields, "off_domain_credit": credit}
        else:
            result, confidence = "fail", 1.0
            evidence = {"fields_checked": rule.condition.fields}

    evaluation = Evaluation(audit_run_id=audit_run_id, rule_id=rule.id, resource_id=None, result=result, confidence=confidence, evidence_json=json.dumps(evidence))
    session.add(evaluation)
    await session.flush()
    return evaluation


async def _evaluate_existing_check(session: AsyncSession, audit_run_id: int, rule: Rule, site_map: SiteMap) -> list[Evaluation]:
    module_path, func_name = rule.condition.check.rsplit(".", 1)
    check_function = getattr(importlib.import_module(module_path), func_name)
    findings = check_function(site_map)

    resource_id_by_url: dict[str, int] = {}
    if any(f.page_url for f in findings):
        rows = (await session.execute(select(Resource).where(Resource.audit_run_id == audit_run_id))).scalars().all()
        resource_id_by_url = {r.url: r.id for r in rows}

    evaluations: list[Evaluation] = []
    for finding in findings:
        result, confidence = _DELEGATED_CONFIDENCE_BY_RESULT[finding.confidence]
        evidence = {"delegated_finding": json.loads(finding.model_dump_json())}
        evaluation = Evaluation(
            audit_run_id=audit_run_id, rule_id=rule.id,
            resource_id=resource_id_by_url.get(finding.page_url) if finding.page_url else None,
            result=result, confidence=confidence, evidence_json=json.dumps(evidence),
        )
        session.add(evaluation)
        evaluations.append(evaluation)

    await session.flush()
    return evaluations


async def evaluate_rule(session: AsyncSession, audit_run_id: int, rule: Rule, site_map: SiteMap) -> list[Evaluation]:
    condition = rule.condition

    if condition.type == ConditionType.CROSS_PAGE_CONTRADICTION:
        evaluation = await evaluate_cross_page_contradiction(
            session, audit_run_id, rule.id, condition.fact, condition.sources or None, rule.min_confidence,
        )
        return [evaluation] if evaluation is not None else []

    if condition.type == ConditionType.NUMERIC_MISMATCH:
        return await evaluate_per_resource_method_comparison(
            session, audit_run_id, rule.id, condition.fact, tuple(condition.methods), condition.tolerance, rule.min_confidence,
        )

    if condition.type == ConditionType.MISSING_RESOURCE:
        evaluation = await _evaluate_missing_resource(session, audit_run_id, rule, site_map)
        return [evaluation] if evaluation is not None else []

    if condition.type == ConditionType.ALL_MISSING:
        evaluation = await _evaluate_all_missing(session, audit_run_id, rule, site_map)
        return [evaluation] if evaluation is not None else []

    if condition.type == ConditionType.EXISTING_CHECK:
        return await _evaluate_existing_check(session, audit_run_id, rule, site_map)

    raise AssertionError(f"unhandled condition type: {condition.type}")  # exhaustive by ConditionType's own definition


async def run_rule_engine(session: AsyncSession, audit_run_id: int, rules: list[Rule], site_map: SiteMap) -> list[Evaluation]:
    all_evaluations: list[Evaluation] = []
    for rule in rules:
        try:
            all_evaluations.extend(await evaluate_rule(session, audit_run_id, rule, site_map))
        except Exception:
            # One rule's own bug/edge case must never abort every other
            # rule's evaluation for this audit - same "a check's own failure
            # degrades gracefully" discipline as capture_annotated_screenshots.
            logger.exception("Rule %r failed to evaluate for audit run %d - skipped, other rules unaffected", rule.id, audit_run_id)

    await session.commit()
    return all_evaluations
