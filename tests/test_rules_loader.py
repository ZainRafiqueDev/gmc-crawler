"""Tests for app.rules.loader: the starter_rules.yaml file parses and
validates cleanly, and sync_rules_to_db always makes the `rules` table match
the YAML exactly (upsert on match, deactivate-not-delete on removal, so
Evaluation/FindingRecord foreign keys never dangle).
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db import Database, RuleRecord
from app.rules.loader import DEFAULT_RULES_PATH, load_rules_from_yaml, sync_rules_to_db
from app.models import ImpactTier
from app.rules.schema import ConditionType, Rule


def test_starter_rules_yaml_parses_and_validates():
    rules = load_rules_from_yaml(DEFAULT_RULES_PATH)
    assert 5 <= len(rules) <= 8
    assert {r.id for r in rules} == {
        "price_mismatch_jsonld_vs_platform",
        "shipping_region_contradiction",
        "return_window_conflict",
        "missing_contact_info",
        "missing_returns_page",
        "business_identity_conflict",
    }


def test_business_identity_rule_delegates_to_existing_check():
    rules = load_rules_from_yaml(DEFAULT_RULES_PATH)
    rule = next(r for r in rules if r.id == "business_identity_conflict")
    assert rule.condition.type == ConditionType.EXISTING_CHECK
    assert rule.condition.check == "app.checks.business_identity.check_business_identity_consistency"


def test_every_rule_has_a_verified_google_source_url_or_an_explicit_null():
    rules = load_rules_from_yaml(DEFAULT_RULES_PATH)
    by_id = {r.id: r.google_source_url for r in rules}

    assert by_id == {
        "price_mismatch_jsonld_vs_platform": "https://support.google.com/merchants/answer/9773429",
        "shipping_region_contradiction": None,  # no verified official page found - flagged, not guessed
        "return_window_conflict": "https://support.google.com/merchants/answer/10220642",
        "missing_contact_info": "https://support.google.com/merchants/answer/10248173",
        "missing_returns_page": "https://support.google.com/merchants/answer/10220642",
        "business_identity_conflict": "https://support.google.com/merchants/answer/6150127",
    }

    for rule in rules:
        if rule.google_source_url is not None:
            assert rule.google_source_url.startswith("https://support.google.com/")


def test_non_official_google_source_domain_fails_loudly(tmp_path):
    bad_yaml = tmp_path / "bad_source.yaml"
    bad_yaml.write_text(
        """
rules:
  - id: bad_source_rule
    scope: site
    inputs: []
    condition: {type: missing_resource, resource_type: faq}
    impact: quality_improvement
    severity: low
    title: bad source rule
    description: test
    remediation: fix it
    google_source_url: "https://some-seo-blog.example.com/google-merchant-policy"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not an official Google domain"):
        load_rules_from_yaml(bad_yaml)


def test_null_google_source_url_is_valid_not_an_error(tmp_path):
    ok_yaml = tmp_path / "null_source.yaml"
    ok_yaml.write_text(
        """
rules:
  - id: null_source_rule
    scope: site
    inputs: []
    condition: {type: missing_resource, resource_type: faq}
    impact: quality_improvement
    severity: low
    title: null source rule
    description: test
    remediation: fix it
""",
        encoding="utf-8",
    )
    rules = load_rules_from_yaml(ok_yaml)
    assert rules[0].google_source_url is None


def test_duplicate_rule_id_raises(tmp_path):
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text(
        """
rules:
  - id: dup
    scope: site
    inputs: []
    condition: {type: missing_resource, resource_type: faq}
    impact: quality_improvement
    severity: low
    title: first rule
    description: first
    remediation: fix it
  - id: dup
    scope: site
    inputs: []
    condition: {type: missing_resource, resource_type: faq}
    impact: quality_improvement
    severity: low
    title: second rule
    description: second
    remediation: fix it
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate rule id"):
        load_rules_from_yaml(bad_yaml)


def _rule(rule_id: str) -> Rule:
    return Rule.model_validate({
        "id": rule_id,
        "scope": "site",
        "inputs": [],
        "condition": {"type": "missing_resource", "resource_type": "faq"},
        "impact": "quality_improvement",
        "severity": "low",
        "title": "test rule",
        "description": "test rule",
        "remediation": "fix it",
    })


@pytest.mark.asyncio
async def test_sync_upserts_and_deactivates_removed_rules(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/rules_test.db")
    await db.init()

    async with db.session() as session:
        await sync_rules_to_db(session, [_rule("rule_a"), _rule("rule_b")])

    async with db.session() as session:
        rows = {r.id: r for r in (await session.execute(select(RuleRecord))).scalars()}
    assert set(rows) == {"rule_a", "rule_b"}
    assert all(r.active for r in rows.values())

    # rule_b removed from the YAML -> row stays (FK-safe) but goes inactive.
    async with db.session() as session:
        await sync_rules_to_db(session, [_rule("rule_a")])

    async with db.session() as session:
        rows = {r.id: r for r in (await session.execute(select(RuleRecord))).scalars()}
    assert set(rows) == {"rule_a", "rule_b"}
    assert rows["rule_a"].active is True
    assert rows["rule_b"].active is False

    # rule_b reappears in the YAML -> reactivated, not duplicated.
    async with db.session() as session:
        await sync_rules_to_db(session, [_rule("rule_a"), _rule("rule_b")])

    async with db.session() as session:
        rows = {r.id: r for r in (await session.execute(select(RuleRecord))).scalars()}
    assert len(rows) == 2
    assert rows["rule_b"].active is True

    await db.dispose()


_ADVISORY_YAML = """
rules:
  - id: {rule_id}
    scope: site
    inputs: []
    condition: {{type: missing_resource, resource_type: faq}}
    impact: {impact}
    severity: low
    title: test rule
    description: test
    remediation: fix it
{extra}"""


def test_starter_rules_load_without_the_null_source_warning(caplog):
    with caplog.at_level("WARNING", logger="gmc_audit.rules.loader"):
        load_rules_from_yaml(DEFAULT_RULES_PATH)
    assert "no verified Google source URL" not in caplog.text


def test_shipping_region_rule_is_an_advisory_with_no_google_claim():
    rule = next(r for r in load_rules_from_yaml(DEFAULT_RULES_PATH) if r.id == "shipping_region_contradiction")
    assert rule.impact == ImpactTier.QUALITY_IMPROVEMENT
    assert rule.policy_reference is None
    assert rule.google_source_url is None
    assert "not a Google Merchant Center policy violation" in rule.description


def test_null_source_on_a_violation_rule_still_warns(tmp_path, caplog):
    path = tmp_path / "violation.yaml"
    path.write_text(_ADVISORY_YAML.format(rule_id="unsourced_violation", impact="listing_disapproval", extra=""), encoding="utf-8")
    with caplog.at_level("WARNING", logger="gmc_audit.rules.loader"):
        load_rules_from_yaml(path)
    assert "unsourced_violation" in caplog.text


def test_advisory_rule_may_not_cite_a_google_rule(tmp_path):
    path = tmp_path / "advisory_with_ref.yaml"
    path.write_text(
        _ADVISORY_YAML.format(rule_id="bad_advisory", impact="quality_improvement", extra='    policy_reference: "Some Google rule"\n'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must not set policy_reference"):
        load_rules_from_yaml(path)
