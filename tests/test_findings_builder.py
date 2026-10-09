"""Tests for app.findings_builder - Evaluation -> FindingRecord conversion
and the compliance-snapshot status rollup.
"""
from __future__ import annotations

import json

import pytest

from app.db import Evaluation
from app.findings_builder import build_finding, compute_snapshot_status
from app.rules.schema import Rule


def _rule(**overrides) -> Rule:
    base = {
        "id": "return_window_conflict", "scope": "site_facts", "inputs": ["return.window_days"],
        "condition": {"type": "cross_page_contradiction", "fact": "return.window_days", "sources": ["returns_policy", "faq"]},
        "impact": "listing_disapproval", "severity": "high", "min_confidence": 0.75,
        "title": "Return window is stated inconsistently across pages",
        "policy_reference": "Shipping and returns policies must be accurate and consistent",
        "description": "test", "remediation": "State the same return window everywhere.",
    }
    base.update(overrides)
    return Rule.model_validate(base)


def _evaluation(result: str, evidence: dict, rule_id: str = "return_window_conflict") -> Evaluation:
    return Evaluation(id=1, audit_run_id=1, rule_id=rule_id, resource_id=None, result=result, confidence=0.9, evidence_json=json.dumps(evidence))


def test_pass_evaluation_produces_no_finding():
    rule = _rule()
    evaluation = _evaluation("pass", {"fact": "return.window_days", "observations": []})
    assert build_finding(rule, evaluation) is None


def test_fail_evaluation_renders_both_sides_of_evidence():
    rule = _rule()
    evidence = {
        "fact": "return.window_days", "scope": "site_wide",
        "observations": [
            {"value": "30", "source_url": "https://example.com/returns", "method": "return_window_regex", "confidence": 0.8, "resource_type": "returns_policy"},
            {"value": "14", "source_url": "https://example.com/faq", "method": "return_window_regex", "confidence": 0.8, "resource_type": "faq"},
        ],
    }
    evaluation = _evaluation("fail", evidence)
    finding = build_finding(rule, evaluation)

    assert finding is not None
    assert finding.title == rule.title
    assert finding.severity == "high"
    assert finding.risk_level == "serious_risk"  # derived from severity=high, not from rule.impact
    assert finding.google_rule == rule.policy_reference
    assert finding.remediation == rule.remediation
    assert "30" in finding.store_evidence and "14" in finding.store_evidence
    assert "returns_policy" in finding.store_evidence and "faq" in finding.store_evidence
    assert "potential suspension" not in finding.consequence  # that's the critical-only wording
    assert "serious risk" in finding.consequence


def test_needs_review_evaluation_still_becomes_a_finding_with_softened_title():
    rule = _rule()
    evidence = {"fact": "return.window_days", "observations": [
        {"value": "30", "source_url": "a", "method": "m", "confidence": 0.5, "resource_type": "returns_policy"},
        {"value": "14", "source_url": "b", "method": "m", "confidence": 0.5, "resource_type": "faq"},
    ]}
    finding = build_finding(rule, _evaluation("needs_review", evidence))
    assert finding is not None
    assert "could not be confirmed with full confidence" in finding.title


def test_critical_severity_uses_suspension_wording_regardless_of_rule_impact():
    """impact is deliberately set to listing_disapproval here (the opposite
    of what severity implies) to prove consequence/risk_level are driven by
    severity alone, never by rule.impact."""
    rule = _rule(impact="listing_disapproval", severity="critical")
    evidence = {"fact": "return.window_days", "observations": [
        {"value": "a", "source_url": "u", "method": "m", "confidence": 0.9, "resource_type": "t"},
        {"value": "b", "source_url": "u2", "method": "m", "confidence": 0.9, "resource_type": "t2"},
    ]}
    finding = build_finding(rule, _evaluation("fail", evidence))
    assert finding.risk_level == "suspension_risk"
    assert "potential suspension risk" in finding.consequence
    assert "will suspend" not in finding.consequence.lower()


@pytest.mark.parametrize("severity,expected_risk_level,expected_phrase", [
    ("critical", "suspension_risk", "potential suspension risk"),
    ("high", "serious_risk", "serious risk"),
    ("medium", "listing_disapproval", "likely disapproval"),
    ("low", "minor_issue", "minor issue"),
])
def test_consequence_and_risk_level_are_a_pure_function_of_severity(severity, expected_risk_level, expected_phrase):
    rule = _rule(severity=severity)
    evidence = {"fact": "return.window_days", "observations": [
        {"value": "a", "source_url": "u", "method": "m", "confidence": 0.9, "resource_type": "t"},
        {"value": "b", "source_url": "u2", "method": "m", "confidence": 0.9, "resource_type": "t2"},
    ]}
    finding = build_finding(rule, _evaluation("fail", evidence))
    assert finding.risk_level == expected_risk_level
    assert expected_phrase in finding.consequence


def test_missing_resource_evidence_renders_absence_clearly():
    rule = _rule(
        id="missing_returns_page",
        condition={"type": "missing_resource", "resource_type": "returns_policy"},
        impact="suspension_risk", severity="critical", title="No returns/refund policy page found",
    )
    evaluation = _evaluation("fail", {"resource_type": "returns_policy", "reachable_count": 0}, rule_id="missing_returns_page")
    finding = build_finding(rule, evaluation)
    assert "No reachable returns_policy page" in finding.store_evidence


def test_blocked_page_evidence_renders_as_could_not_verify_not_confirmed_missing():
    rule = _rule(
        id="missing_returns_page",
        condition={"type": "missing_resource", "resource_type": "returns_policy"},
        impact="suspension_risk", severity="critical", title="No returns/refund policy page found",
    )
    evidence = {
        "resource_type": "returns_policy",
        "blocked_or_unreachable_pages": [{"url": "https://example.com/returns", "failure_category": "bot_blocked"}],
    }
    evaluation = _evaluation("needs_review", evidence, rule_id="missing_returns_page")
    finding = build_finding(rule, evaluation)

    assert finding is not None
    assert "could not be confirmed with full confidence" in finding.title
    assert "https://example.com/returns" in finding.store_evidence
    assert "not a confirmed absence" in finding.store_evidence
    assert finding.page_url == "https://example.com/returns"


def test_all_missing_evidence_lists_the_checked_fields():
    rule = _rule(
        id="missing_contact_info", inputs=["business.email", "business.phone"],
        condition={"type": "all_missing", "fields": ["business.email", "business.phone"]},
        impact="suspension_risk", severity="critical", title="No business contact information found",
    )
    evaluation = _evaluation("fail", {"fields_checked": ["business.email", "business.phone"]}, rule_id="missing_contact_info")
    finding = build_finding(rule, evaluation)
    assert "business.email" in finding.store_evidence and "business.phone" in finding.store_evidence


def test_existing_check_delegated_finding_uses_the_delegated_fields():
    rule = _rule(
        id="business_identity_conflict", condition={"type": "existing_check", "check": "app.checks.business_identity.check_business_identity_consistency"},
        impact="suspension_risk", severity="critical", title="Business identity is inconsistent across pages",
    )
    delegated = {
        "check_id": "business_identity_email_consistency", "title": "Multiple different contact emails found across the site",
        "severity": "medium", "evidence": "Found 2 distinct email addresses", "policy_reference": "GMC: specific policy text",
        "recommended_fix": "Pick one support email and use it everywhere.",
    }
    evaluation = _evaluation("fail", {"delegated_finding": delegated}, rule_id="business_identity_conflict")
    finding = build_finding(rule, evaluation)

    assert finding.check_id == "business_identity_email_consistency"
    assert finding.title == delegated["title"]
    assert finding.severity == "medium"  # delegated check's own severity, not the rule's "critical"
    assert finding.store_evidence == delegated["evidence"]
    assert finding.remediation == delegated["recommended_fix"]

    # The exact inconsistency this round fixed: a medium-severity delegated
    # finding must never inherit its parent rule's suspension_risk framing
    # (the rule is severity="critical"/impact="suspension_risk" - calibrated
    # for its own worst case, e.g. "no contact info at all" - not for this
    # specific, milder finding).
    assert finding.risk_level == "listing_disapproval"
    assert "potential suspension risk" not in finding.consequence  # may mention "not a suspension-level issue" as reassurance, never assert it as the claim
    assert "likely disapproval" in finding.consequence


def test_two_phone_numbers_finding_gets_listing_disapproval_not_suspension_risk():
    """Regression test for the live-run finding caught in review: the real
    business_identity_phone_consistency finding (severity=medium) must read
    as a listing/eligibility-tier consequence, not a suspension-tier one -
    Google's own enforcement for a simple, possibly-benign (e.g. regional
    support lines) multi-phone-number inconsistency is a data-quality/trust
    signal affecting listing-level scrutiny, not on its own grounds for
    account suspension (that requires a clearer misrepresentation signal,
    e.g. no verifiable business identity at all - see missing_contact_info's
    own severity=critical for that case)."""
    rule = _rule(
        id="business_identity_conflict", condition={"type": "existing_check", "check": "app.checks.business_identity.check_business_identity_consistency"},
        impact="suspension_risk", severity="critical", title="Business identity is inconsistent across pages",
    )
    delegated = {
        "check_id": "business_identity_phone_consistency", "title": "Multiple different phone numbers found across the site",
        "severity": "medium", "evidence": "Found 2 distinct phone numbers: +18664485031 (on .../); +447760858897 (on .../about-us)",
        "policy_reference": "GMC: Business contact information must be consistent and accurate",
        "recommended_fix": "Confirm whether these are intentionally different (e.g. regional support lines) or inconsistent/outdated.",
    }
    evaluation = _evaluation("fail", {"delegated_finding": delegated}, rule_id="business_identity_conflict")
    finding = build_finding(rule, evaluation)

    assert finding.risk_level == "listing_disapproval"
    assert "potential suspension risk" not in finding.consequence  # may mention "not a suspension-level issue" as reassurance, never assert it as the claim


def test_google_source_link_populated_from_rule_when_present():
    rule = _rule(google_source_url="https://support.google.com/merchants/answer/10220642")
    evidence = {"fact": "return.window_days", "observations": [
        {"value": "30", "source_url": "u", "method": "m", "confidence": 0.9, "resource_type": "t"},
        {"value": "14", "source_url": "u2", "method": "m", "confidence": 0.9, "resource_type": "t2"},
    ]}
    finding = build_finding(rule, _evaluation("fail", evidence))
    assert finding.google_source_link == "https://support.google.com/merchants/answer/10220642"


def test_google_source_link_is_none_when_rule_has_no_verified_source():
    rule = _rule(google_source_url=None)
    evidence = {"fact": "return.window_days", "observations": [
        {"value": "30", "source_url": "u", "method": "m", "confidence": 0.9, "resource_type": "t"},
        {"value": "14", "source_url": "u2", "method": "m", "confidence": 0.9, "resource_type": "t2"},
    ]}
    finding = build_finding(rule, _evaluation("fail", evidence))
    assert finding.google_source_link is None


def test_delegated_finding_also_gets_the_rules_google_source_link():
    rule = _rule(
        id="business_identity_conflict", condition={"type": "existing_check", "check": "app.checks.business_identity.check_business_identity_consistency"},
        impact="suspension_risk", severity="critical", title="Business identity is inconsistent across pages",
        google_source_url="https://support.google.com/merchants/answer/6150127",
    )
    delegated = {
        "check_id": "business_identity_email_consistency", "title": "Multiple different contact emails found across the site",
        "severity": "medium", "evidence": "Found 2 distinct email addresses", "policy_reference": "GMC: specific policy text",
        "recommended_fix": "Pick one support email and use it everywhere.",
    }
    evaluation = _evaluation("fail", {"delegated_finding": delegated}, rule_id="business_identity_conflict")
    finding = build_finding(rule, evaluation)
    assert finding.google_source_link == "https://support.google.com/merchants/answer/6150127"


def test_compute_snapshot_status_empty_is_compliant():
    assert compute_snapshot_status([]) == "COMPLIANT"


@pytest.mark.parametrize("severities,expected", [
    (["critical"], "CRITICAL"),
    (["high"], "AT_RISK"),
    (["medium"], "ACTION_REQUIRED"),
    (["low"], "ACTION_REQUIRED"),
    (["medium", "critical"], "CRITICAL"),  # highest severity wins regardless of order
    (["low", "high"], "AT_RISK"),
])
def test_compute_snapshot_status_picks_highest_severity(severities, expected):
    class _F:
        def __init__(self, severity):
            self.severity = severity
    findings = [_F(s) for s in severities]
    assert compute_snapshot_status(findings) == expected


def _advisory_rule() -> Rule:
    return _rule(
        id="shipping_region_contradiction", impact="quality_improvement", severity="low",
        policy_reference=None, title="Your pages state different shipping regions",
        condition={"type": "cross_page_contradiction", "fact": "shipping.region", "sources": ["homepage", "shipping_policy"]},
    )


def _advisory_finding():
    evidence = {
        "fact": "shipping.region", "scope": "site_wide",
        "observations": [
            {"value": "Worldwide", "source_url": "https://example.com/", "method": "shipping_region_regex", "confidence": 0.8, "resource_type": "homepage"},
            {"value": "US only", "source_url": "https://example.com/shipping", "method": "shipping_region_regex", "confidence": 0.8, "resource_type": "shipping_policy"},
        ],
    }
    return build_finding(_advisory_rule(), _evaluation("fail", evidence, rule_id="shipping_region_contradiction"))


def test_quality_improvement_rule_produces_an_advisory_with_no_google_claim():
    finding = _advisory_finding()
    assert finding.risk_level == "advisory"
    assert finding.google_rule is None
    assert finding.google_source_link is None
    assert "not a Google Merchant Center policy violation" in finding.consequence
    assert "Worldwide" in finding.store_evidence and "US only" in finding.store_evidence


def test_advisory_alone_leaves_snapshot_compliant():
    assert compute_snapshot_status([_advisory_finding()]) == "COMPLIANT"


def test_advisory_does_not_mask_a_real_risk_finding():
    class _F:
        severity, risk_level = "high", "serious_risk"
    assert compute_snapshot_status([_advisory_finding(), _F()]) == "AT_RISK"


def test_report_puts_advisories_in_their_own_section_without_a_google_rule_line():
    from app.db import FirstAuditRun
    from app.first_audit_report import render_first_audit_report_markdown

    run = FirstAuditRun(url="https://example.com", status="done", snapshot_status="COMPLIANT", pages_crawled=5)
    markdown = render_first_audit_report_markdown(run, [_advisory_finding()])

    assert "## No actionable risks found" in markdown
    assert "## Advisories (1)" in markdown
    assert "Advisory - not a Google policy violation" in markdown
    assert "Google rule" not in markdown
    assert "Risk level" not in markdown
