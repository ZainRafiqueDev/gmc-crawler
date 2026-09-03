from __future__ import annotations

from app.models import Confidence, Finding, Severity
from validation.category_mapping import CATEGORIES, CHECK_ID_TO_CATEGORY, category_for_check_id
from validation.score_validation import tool_verdict_for_category


def _finding(check_id: str, confidence: Confidence = Confidence.CONFIRMED) -> Finding:
    return Finding(
        check_id=check_id, title="t", severity=Severity.HIGH, confidence=confidence, evidence="e",
    )


def test_no_check_id_is_mapped_to_more_than_one_category():
    seen: dict[str, str] = {}
    for category in CATEGORIES:
        from validation.category_mapping import CATEGORY_CHECK_IDS

        for check_id in CATEGORY_CHECK_IDS[category]:
            assert check_id not in seen, f"{check_id} mapped to both {seen.get(check_id)} and {category}"
            seen[check_id] = category


def test_category_for_check_id_known_and_unknown():
    assert category_for_check_id("external_domain_link") == "external_domain_links"
    assert category_for_check_id("not_a_real_check_id") is None


def test_all_mapped_check_ids_resolve_via_chick_id_to_category():
    for check_id, category in CHECK_ID_TO_CATEGORY.items():
        assert category_for_check_id(check_id) == category


def test_tool_verdict_fail_when_confirmed_finding_present():
    findings = [_finding("external_domain_link")]
    assert tool_verdict_for_category(findings, "external_domain_links") == "fail"


def test_tool_verdict_pass_when_no_relevant_findings():
    findings = [_finding("external_domain_link")]
    assert tool_verdict_for_category(findings, "product_image_issues") == "pass"


def test_tool_verdict_cannot_verify_when_only_cannot_verify_findings():
    findings = [_finding("llm_editorial_quality", confidence=Confidence.CANNOT_VERIFY)]
    assert tool_verdict_for_category(findings, "policy_substance_quality") == "cannot_verify"


def test_tool_verdict_fail_when_mixed_cannot_verify_and_confirmed():
    findings = [
        _finding("llm_editorial_quality", confidence=Confidence.CANNOT_VERIFY),
        _finding("llm_prohibited_content", confidence=Confidence.POTENTIAL_RISK),
    ]
    assert tool_verdict_for_category(findings, "prohibited_content_risk") == "fail"
