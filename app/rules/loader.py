"""Loads app/rules/starter_rules.yaml (the source of truth), validates every
rule against app.rules.schema.Rule, and syncs a mirror copy into the `rules`
DB table so app.db.Evaluation/FindingRecord rows have a stable foreign key.

The DB table is never authoritative - sync_rules_to_db always makes the table
match the YAML exactly: every YAML rule is upserted, and any DB row whose id
is no longer present in the YAML is marked inactive (not deleted, so a
historical Evaluation/FindingRecord that references it keeps a valid FK).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urlparse

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import RuleRecord
from app.facts.keys import FACT_REGISTRY, METHOD_BASE_CONFIDENCE
from app.models import ImpactTier, PageType
from app.rules.schema import ConditionType, Rule

logger = logging.getLogger("gmc_audit.rules.loader")

DEFAULT_RULES_PATH = Path(__file__).parent / "starter_rules.yaml"

_VALID_RESOURCE_TYPES = {pt.value for pt in PageType}

# "Must be the page that actually states the requirement" - official Google
# domains only. A rule with a google_source_url outside this allowlist (a
# blog, an SEO article, a third-party mirror) fails loudly at load time
# rather than silently citing an unofficial source.
_ALLOWED_GOOGLE_SOURCE_HOSTS = {"support.google.com", "google.com", "www.google.com", "developers.google.com"}


def _validate_google_source_url(rule_id: str, url: str | None) -> None:
    if url is None:
        return  # explicit null - no verified source found, not a validation failure
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc not in _ALLOWED_GOOGLE_SOURCE_HOSTS:
        raise ValueError(
            f"rule {rule_id!r}: google_source_url={url!r} is not an official Google domain "
            f"(must be https:// and one of {sorted(_ALLOWED_GOOGLE_SOURCE_HOSTS)}, or null)"
        )


def _validate_advisory(rule: Rule) -> None:
    """A quality_improvement rule is an advisory: it makes no Google-violation
    claim, so it may have a null google_source_url (only violation rules are
    expected to cite one) - but it must then not cite a Google rule either.
    A policy_reference here would print as "Google rule: ..." on a finding
    that has no Google source behind it.
    """
    if rule.impact == ImpactTier.QUALITY_IMPROVEMENT and rule.policy_reference is not None:
        raise ValueError(
            f"rule {rule.id!r}: impact=quality_improvement is an advisory and must not set "
            f"policy_reference (it would be presented as a Google rule) - promote the rule to a "
            f"violation tier with a verified google_source_url instead"
        )


def _validate_fact_key(rule_id: str, fact_key: str | None, field_name: str) -> None:
    if fact_key is not None and fact_key not in FACT_REGISTRY:
        raise ValueError(
            f"rule {rule_id!r}: condition.{field_name}={fact_key!r} is not a known fact key "
            f"(see app/facts/canonical_facts.md) - this is exactly the dead-key state the "
            f"rename+validation were locked together to prevent"
        )


def _validate_resource_type(rule_id: str, resource_type: str | None, field_name: str) -> None:
    if resource_type is not None and resource_type not in _VALID_RESOURCE_TYPES:
        raise ValueError(f"rule {rule_id!r}: condition.{field_name}={resource_type!r} is not a known PageType value")


def _validate_rule_against_registry(rule: Rule) -> None:
    """Every fact key / resource_type / method a rule's condition references
    must exist in the canonical fact dictionary - the key-existence check
    deliberately deferred until the rename shipped (see decisions.md), so
    there is never a window where a rule loads cleanly but references a key
    nothing will ever produce.
    """
    condition = rule.condition

    if condition.type in (ConditionType.CROSS_PAGE_CONTRADICTION, ConditionType.NUMERIC_MISMATCH):
        _validate_fact_key(rule.id, condition.fact, "fact")

    for source in condition.sources:
        _validate_resource_type(rule.id, source, "sources")

    if condition.type == ConditionType.MISSING_RESOURCE:
        _validate_resource_type(rule.id, condition.resource_type, "resource_type")

    for field_key in condition.fields:
        _validate_fact_key(rule.id, field_key, "fields")

    for method in condition.methods:
        if method not in METHOD_BASE_CONFIDENCE:
            raise ValueError(f"rule {rule.id!r}: condition.methods references unknown method {method!r}")


def load_rules_from_yaml(path: Path = DEFAULT_RULES_PATH) -> list[Rule]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules_raw = raw.get("rules", []) if raw else []
    rules = [Rule.model_validate(r) for r in rules_raw]

    ids = [r.id for r in rules]
    duplicates = {rule_id for rule_id in ids if ids.count(rule_id) > 1}
    if duplicates:
        raise ValueError(f"duplicate rule id(s) in {path}: {sorted(duplicates)}")

    for rule in rules:
        _validate_rule_against_registry(rule)
        _validate_google_source_url(rule.id, rule.google_source_url)
        _validate_advisory(rule)

    # Advisories (quality_improvement) make no Google-violation claim, so a
    # null source is expected for them - only a violation-tier rule without a
    # verified source is worth warning about.
    unsourced = [r.id for r in rules if r.google_source_url is None and r.impact != ImpactTier.QUALITY_IMPROVEMENT]
    if unsourced:
        logger.warning("%d violation rule(s) have no verified Google source URL (google_source_url=null): %s", len(unsourced), unsourced)

    return rules


async def sync_rules_to_db(session: AsyncSession, rules: list[Rule]) -> None:
    yaml_ids = {r.id for r in rules}

    existing = {row.id: row for row in (await session.execute(select(RuleRecord))).scalars()}

    for rule in rules:
        payload = dict(
            scope=rule.scope,
            inputs_json=json.dumps(rule.inputs),
            condition_json=rule.condition.model_dump_json(),
            impact=rule.impact.value,
            severity=rule.severity.value,
            description=rule.description,
            min_confidence=rule.min_confidence,
            policy_reference=rule.policy_reference,
            google_source_url=rule.google_source_url,
            active=True,
        )
        row = existing.get(rule.id)
        if row is None:
            session.add(RuleRecord(id=rule.id, **payload))
        else:
            for key, value in payload.items():
                setattr(row, key, value)

    for rule_id, row in existing.items():
        if rule_id not in yaml_ids and row.active:
            row.active = False

    await session.commit()
