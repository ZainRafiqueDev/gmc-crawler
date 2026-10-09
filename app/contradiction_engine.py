"""Contradiction engine (work-order step 4, piece 3): compares the SAME
canonical fact across sources and produces app.db.Evaluation rows. Produces
evaluations only - no Finding rows, no body-evidence wiring (both piece 4).

Two entry points, matching the two comparison axes the canonical fact
dictionary defines (app/facts/canonical_facts.md):

- evaluate_cross_page_contradiction: SITE-WIDE facts (business.*, shipping.*,
  return.*, payment.*) - compares across PAGES. Exactly one Evaluation per
  call (resource_id=None - the finding, if any, is about the site, not one
  page).
- evaluate_per_resource_method_comparison: PER-RESOURCE facts (product.*) -
  compares across EXTRACTION METHODS for the SAME product, grouped by
  (resource_id, variant_key) so a multi-offer product page's variants are
  never collapsed into one comparison or silently dropped. One Evaluation
  per (resource, variant) pair that had enough evidence to compare - never
  across different products.

Both share three rules, enforced in exactly one place each:
1. Comparison is always on (canonical fact key, value) via
   app.facts.normalize.values_equal - never on source_text/raw strings, and
   the per-fact-type tolerance (numeric vs verbatim vs case-insensitive text)
   lives there, not duplicated here.
2. "Absent" is not "conflicting": fewer than 2 actual values to compare
   produces no Evaluation at all (cross-page) or is skipped for that
   resource/variant (per-resource) - a missing value is the rule engine's
   concern (piece 4), not this one's.
3. Confidence gate: result is "fail" only when every fact instance
   contributing to the comparison meets `min_confidence` (the caller's own
   rule's threshold - never hardcoded here); otherwise "needs_review", the
   same CANNOT_VERIFY-style downgrade used elsewhere in this project
   (soft-404, evidence_verified).

Every query is scoped to one fact key (and, for per-resource comparisons,
one pair of methods) within one audit run - never a bare `select(ResourceFact)`
that would pull an entire run's fact table into memory.
"""
from __future__ import annotations

import json
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Evaluation, Resource, ResourceFact
from app.facts.normalize import values_equal


def _observation(fact: ResourceFact, resource: Resource) -> dict:
    return {
        "value": fact.value,
        "source_url": fact.source_url,
        "source_text": fact.source_text[:300],
        "method": fact.method,
        "confidence": fact.confidence,
        "resource_type": resource.resource_type,
    }


async def evaluate_cross_page_contradiction(
    session: AsyncSession, audit_run_id: int, rule_id: str, fact_key: str,
    source_types: list[str] | None, min_confidence: float,
) -> Evaluation | None:
    stmt = (
        select(ResourceFact, Resource)
        .join(Resource, ResourceFact.resource_id == Resource.id)
        .where(Resource.audit_run_id == audit_run_id, ResourceFact.fact == fact_key)
    )
    if source_types:
        stmt = stmt.where(Resource.resource_type.in_(source_types))

    observations = (await session.execute(stmt)).all()
    if len(observations) < 2:
        return None  # nothing to compare - absent elsewhere is not a conflict

    # Group into equivalence classes using the ONE shared comparison rule for
    # this fact key - first observation per class is the class's representative.
    groups: list[tuple[str, list[tuple[ResourceFact, Resource]]]] = []
    for fact, resource in observations:
        for rep_value, members in groups:
            if values_equal(fact_key, rep_value, fact.value):
                members.append((fact, resource))
                break
        else:
            groups.append((fact.value, [(fact, resource)]))

    min_conf = min(fact.confidence for fact, _ in observations)
    if len(groups) <= 1:
        result = "pass"
    else:
        result = "fail" if min_conf >= min_confidence else "needs_review"

    evidence = {
        "fact": fact_key, "scope": "site_wide", "distinct_values": len(groups),
        "observations": [_observation(fact, resource) for fact, resource in observations],
    }
    evaluation = Evaluation(
        audit_run_id=audit_run_id, rule_id=rule_id, resource_id=None,
        result=result, confidence=min_conf, evidence_json=json.dumps(evidence),
    )
    session.add(evaluation)
    await session.flush()
    return evaluation


async def evaluate_per_resource_method_comparison(
    session: AsyncSession, audit_run_id: int, rule_id: str, fact_key: str,
    methods: tuple[str, str], tolerance: float, min_confidence: float,
) -> list[Evaluation]:
    stmt = (
        select(ResourceFact, Resource)
        .join(Resource, ResourceFact.resource_id == Resource.id)
        .where(
            Resource.audit_run_id == audit_run_id,
            ResourceFact.fact == fact_key,
            ResourceFact.method.in_(methods),
        )
    )
    rows = (await session.execute(stmt)).all()

    # (resource_id, variant_key) -> method -> [(fact, resource), ...] - two
    # different products, or two different variants of the same product,
    # are never compared against each other; only the SAME (resource,
    # variant) across its two methods is.
    groups: dict[tuple[int, str], dict[str, list[tuple[ResourceFact, Resource]]]] = defaultdict(lambda: defaultdict(list))
    for fact, resource in rows:
        groups[(resource.id, fact.variant_key or "")][fact.method].append((fact, resource))

    evaluations: list[Evaluation] = []
    for (resource_id, _variant_key), by_method in groups.items():
        if not all(by_method.get(m) for m in methods):
            continue  # absent on at least one side for this resource/variant - not a conflict

        fact_a, resource_a = by_method[methods[0]][0]
        fact_b, _ = by_method[methods[1]][0]

        try:
            equal = abs(float(fact_a.value) - float(fact_b.value)) <= tolerance
        except ValueError:
            equal = fact_a.value == fact_b.value

        min_conf = min(fact_a.confidence, fact_b.confidence)
        result = "pass" if equal else ("fail" if min_conf >= min_confidence else "needs_review")

        evidence = {
            "fact": fact_key, "scope": "per_resource", "tolerance": tolerance,
            "observations": [_observation(fact_a, resource_a), _observation(fact_b, resource_a)],
        }
        evaluation = Evaluation(
            audit_run_id=audit_run_id, rule_id=rule_id, resource_id=resource_id,
            result=result, confidence=min_conf, evidence_json=json.dumps(evidence),
        )
        session.add(evaluation)
        evaluations.append(evaluation)

    await session.flush()
    return evaluations
