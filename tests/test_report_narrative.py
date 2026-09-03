"""Tests for the narrative-sophistication restructuring (Parts 3-5):
grouped catalog page-by-page section, Policy-by-Policy Review matrix, the
explainable internal risk score, richer per-finding structure, and the
API-verification-gap recommendation."""
from datetime import datetime, timezone

from app.impact_tier import policy_area_for_finding, tier_for_check_id
from app.models import (
    AdsEligibilityImpact, Confidence, CrawledPage, Finding, ImpactTier, LLMCoverageStats, PageType, Platform,
    PlatformDetectionResult, Severity, SiteMap,
)
from app.report import compute_risk_score, generate_markdown_report

_PLATFORM_WOOCOMMERCE = PlatformDetectionResult(
    platform=Platform.WOOCOMMERCE, base_url="https://shop.example/",
    evidence=["/wp-json/wc/v3/products returned 403 (route exists)"],
)
_PLATFORM_UNKNOWN = PlatformDetectionResult(platform=Platform.UNKNOWN, base_url="https://shop.example/", evidence=[])


def _finding(
    check_id: str, severity: Severity, page_url: str | None = "https://shop.example/",
    confidence: Confidence = Confidence.CONFIRMED, title: str | None = None,
    policy_reference: str | None = None, policy_requirement_text: str | None = None,
    ads_eligibility_impact: AdsEligibilityImpact = AdsEligibilityImpact.UNCLEAR,
    policy_last_verified: datetime | None = None,
) -> Finding:
    return Finding(
        check_id=check_id, title=title or f"title-{check_id}", severity=severity, confidence=confidence,
        page_url=page_url, evidence="evidence text", impact_tier=tier_for_check_id(check_id),
        policy_reference=policy_reference, policy_requirement_text=policy_requirement_text,
        ads_eligibility_impact=ads_eligibility_impact, policy_last_verified=policy_last_verified,
    )


def _page(url: str, page_type: PageType, depth: int = 0) -> CrawledPage:
    return CrawledPage(url=url, page_type=page_type, depth=depth, reachable=True)


def _site_map(*pages: CrawledPage) -> SiteMap:
    pages = pages or (_page("https://shop.example/", PageType.HOMEPAGE),)
    return SiteMap(base_url="https://shop.example/", pages=list(pages))


# --- Policy-by-Policy Review matrix ---------------------------------------

def test_policy_area_attribution_for_business_identity_and_substance_checks():
    assert policy_area_for_finding(_finding("business_identity_present", Severity.CRITICAL)) == "business_identity"
    assert policy_area_for_finding(_finding("llm_policy_substance_privacy_policy", Severity.HIGH)) == "privacy_policy"
    assert policy_area_for_finding(_finding("llm_prohibited_content", Severity.CRITICAL)) == "prohibited_content"


def test_required_page_present_attribution_comes_from_title():
    f = _finding("required_page_present", Severity.CRITICAL, title="Missing required page: Shipping policy")
    assert policy_area_for_finding(f) == "shipping_policy"
    f2 = _finding("required_page_present", Severity.CRITICAL, title="Missing required page: Contact info")
    assert policy_area_for_finding(f2) == "business_identity"


def test_ungrounded_check_has_no_policy_area():
    assert policy_area_for_finding(_finding("broken_internal_link", Severity.LOW)) is None


def test_policy_matrix_shows_pass_for_area_with_no_findings():
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(), [])
    assert "| Shipping Policy | Pass | 0 |" in report


def test_policy_matrix_shows_fail_for_area_with_a_confirmed_finding():
    """Follow-up round, Part 2: confidence-aware status - a CONFIRMED
    finding is a real, confirmed problem and must read as "Fail", distinct
    from a merely-flagged "At Risk" (see the next two tests)."""
    findings = [_finding("llm_prohibited_content", Severity.CRITICAL, title="Potentially prohibited product content")]
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(), findings)
    assert "| Prohibited Content | Fail | 1 |" in report


def test_policy_matrix_shows_at_risk_for_area_with_only_potential_risk_findings():
    """A policy area with nothing but POTENTIAL_RISK findings (e.g. dozens
    of external links) must not read the same as one with a confirmed
    problem - "At Risk", not "Fail"."""
    findings = [_finding("llm_prohibited_content", Severity.MEDIUM, title="Possibly prohibited content", confidence=Confidence.POTENTIAL_RISK)]
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(), findings)
    assert "| Prohibited Content | At Risk | 1 |" in report


def test_policy_matrix_confirmed_finding_outranks_potential_risk_findings_in_the_same_area():
    findings = [
        _finding("llm_prohibited_content", Severity.MEDIUM, title="Possibly prohibited content", confidence=Confidence.POTENTIAL_RISK),
        _finding("llm_prohibited_content", Severity.CRITICAL, title="Confirmed prohibited content"),
    ]
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(), findings)
    assert "| Prohibited Content | Fail | 2 |" in report
    assert "Confirmed prohibited content" in report.split("## Policy-by-Policy Review")[1].split("## Other Findings")[0]


def test_policy_matrix_shows_cannot_verify_when_only_unconfirmed_findings_exist():
    findings = [_finding("llm_editorial_quality", Severity.LOW, confidence=Confidence.CANNOT_VERIFY)]
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(), findings)
    assert "| Editorial Quality | Cannot Verify | 1 |" in report


# --- Fixed-size LLM catalog sampling honesty (follow-up round) -------------

def test_at_a_glance_shows_coverage_line_when_partial():
    coverage = LLMCoverageStats(llm_configured=True, total_reachable_product_pages=214, product_pages_checked=5)
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(), [], llm_coverage=coverage)
    assert "Prohibited-content / editorial-quality screening: 5 of 214 product page(s) checked (2%)" in report


def test_at_a_glance_omits_coverage_line_when_not_supplied():
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(), [])
    assert "Prohibited-content / editorial-quality screening" not in report


def test_at_a_glance_coverage_line_when_llm_not_configured():
    coverage = LLMCoverageStats(llm_configured=False, total_reachable_product_pages=50, product_pages_checked=0)
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(), [], llm_coverage=coverage)
    assert "not run for any of 50 product page(s) - no LLM provider configured" in report


def test_at_a_glance_omits_coverage_line_when_store_has_no_products():
    coverage = LLMCoverageStats(llm_configured=True, total_reachable_product_pages=0, product_pages_checked=0)
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(), [], llm_coverage=coverage)
    assert "Prohibited-content / editorial-quality screening" not in report


def test_policy_matrix_annotates_pass_with_partial_coverage():
    """A "Pass" earned from a fixed-size sample must not read the same as
    a fully-checked one - a 2%-sampled Pass on a 500-product store is not
    the same claim as a 100%-sampled one."""
    coverage = LLMCoverageStats(llm_configured=True, total_reachable_product_pages=214, product_pages_checked=5)
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(), [], llm_coverage=coverage)
    assert "| Prohibited Content | Pass (partial coverage: 5/214) | 0 |" in report
    assert "| Editorial Quality | Pass (partial coverage: 5/214) | 0 |" in report
    # Unrelated areas (not covered by these two LLM checks) stay a plain Pass.
    assert "| Shipping Policy | Pass | 0 |" in report


def test_policy_matrix_does_not_annotate_a_confirmed_fail_with_coverage():
    """A Fail already tells the reader something concrete was found - the
    coverage caveat only matters for a Pass, which could otherwise read as
    a clean sweep of the whole catalog. (Editorial Quality, untouched by
    this finding, correctly still shows its own partial-coverage Pass.)"""
    coverage = LLMCoverageStats(llm_configured=True, total_reachable_product_pages=214, product_pages_checked=5)
    findings = [_finding("llm_prohibited_content", Severity.CRITICAL, title="Potentially prohibited product content")]
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(), findings, llm_coverage=coverage)
    assert "| Prohibited Content | Fail | 1 |" in report
    assert "| Editorial Quality | Pass (partial coverage: 5/214) | 0 |" in report


def test_policy_matrix_no_partial_annotation_when_catalog_fits_the_sample():
    coverage = LLMCoverageStats(llm_configured=True, total_reachable_product_pages=3, product_pages_checked=3)
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(), [], llm_coverage=coverage)
    assert "| Prohibited Content | Pass | 0 |" in report
    assert "partial coverage" not in report


# --- Internal risk score ---------------------------------------------------

def test_no_findings_scores_100_low_risk():
    result = compute_risk_score([])
    assert result.score == 100
    assert result.rating == "LOW"


def test_single_critical_suspension_finding_forces_high_rating():
    findings = [_finding("required_page_present", Severity.CRITICAL)]
    result = compute_risk_score(findings)
    assert result.rating == "HIGH"
    assert result.score < 100
    assert any("suspension-risk" in line for line in result.breakdown)


def test_cannot_verify_findings_are_excluded_from_score_deductions():
    findings = [_finding("required_page_present", Severity.CRITICAL, confidence=Confidence.CANNOT_VERIFY)]
    result = compute_risk_score(findings)
    assert result.score == 100


def test_cannot_verify_exclusion_is_stated_explicitly_in_the_breakdown():
    """Follow-up round, Part 3: a real report's breakdown summed to 150
    findings out of 170 total with no stated reason for the gap - a reader
    had to already know CANNOT_VERIFY findings are excluded by design."""
    findings = [
        _finding("required_page_present", Severity.CRITICAL),
        _finding("broken_internal_link", Severity.LOW, confidence=Confidence.CANNOT_VERIFY),
        _finding("form_action_unreachable", Severity.MEDIUM, confidence=Confidence.CANNOT_VERIFY),
    ]
    result = compute_risk_score(findings)
    assert any("2 finding(s) excluded from scoring: cannot-verify" in line for line in result.breakdown)


def test_no_exclusion_line_when_nothing_was_excluded():
    findings = [_finding("required_page_present", Severity.CRITICAL)]
    result = compute_risk_score(findings)
    assert not any("excluded from scoring" in line for line in result.breakdown)


def test_heavily_negative_raw_score_shows_the_floor_explicitly():
    """Follow-up round, Part 2: a real report's itemized deductions summed
    to -195 with a displayed score of 0/100 and no stated floor - the
    report claims to be "reproducible from its own findings," which wasn't
    true without this line."""
    findings = [_finding(f"required_page_present_{i}", Severity.CRITICAL) for i in range(10)]
    result = compute_risk_score(findings)
    assert result.score == 0
    assert any(line.startswith("Raw score before clamping:") and "floored to 0" in line for line in result.breakdown)


def test_score_of_exactly_zero_without_clamping_shows_no_floor_line():
    """A raw score that lands on exactly 0 with no clamping needed must not
    claim a floor was applied - only state it when the clamp actually did
    something."""
    # 4 critical suspension-risk findings at 25pts each = exactly -100 -> 0,
    # with no negative headroom below that to have been clamped away.
    findings = [_finding(f"required_page_present_{i}", Severity.CRITICAL) for i in range(4)]
    result = compute_risk_score(findings)
    assert result.score == 0
    assert not any(line.startswith("Raw score before clamping:") for line in result.breakdown)


def test_risk_score_disclaimer_present_in_report():
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(), [])
    assert "not a Google score" in report
    assert "not a prediction of actual approval or suspension" in report


# --- Richer per-finding structure ------------------------------------------

def test_suspension_finding_rendered_with_rich_fields():
    findings = [_finding(
        "llm_prohibited_content", Severity.CRITICAL,
        policy_reference='GMC policy: Prohibited content [prohibited_content] - https://support.google.com/merchants/answer/6149970 ("Prohibited content")',
        policy_requirement_text="Google does not allow ads or destinations that display shocking content.",
    )]
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(), findings, major_only=True)
    suspension_section = report.split("## Suspension Risk Findings")[1].split("## Policy-by-Policy Review")[0]

    assert "**Why It Matters:**" in suspension_section
    assert "**Official Source:** https://support.google.com/merchants/answer/6149970" in suspension_section
    assert "**Specific Policy Requirement:** \"Google does not allow ads" in suspension_section


def test_finding_without_policy_requirement_text_omits_that_field_gracefully():
    findings = [_finding("required_page_present", Severity.CRITICAL, policy_reference="GMC: Store must have a shipping policy")]
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(), findings, major_only=True)
    suspension_section = report.split("## Suspension Risk Findings")[1]
    assert "Specific Policy Requirement" not in suspension_section
    assert "Official Source" not in suspension_section  # no URL embedded in this deterministic check's policy_reference


# --- Part 1 (ads-eligibility tagging) and Part 4 (citation freshness) -----

def test_suspension_finding_shows_ads_eligibility_impact_and_verified_date():
    findings = [_finding(
        "llm_prohibited_content", Severity.CRITICAL,
        policy_reference='GMC policy: Prohibited content [prohibited_content] - https://support.google.com/merchants/answer/6149970 ("Prohibited content")',
        policy_requirement_text="Google does not allow ads or destinations that display shocking content.",
        ads_eligibility_impact=AdsEligibilityImpact.ADS_AND_LISTINGS,
        policy_last_verified=datetime(2026, 8, 15, tzinfo=timezone.utc),
    )]
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(), findings, major_only=True)
    suspension_section = report.split("## Suspension Risk Findings")[1].split("## Policy-by-Policy Review")[0]

    assert "**Ads Eligibility Impact:** Affects both paid Shopping ads and free-listing eligibility" in suspension_section
    assert "**Official Source:** https://support.google.com/merchants/answer/6149970 (last verified: 2026-08-15)" in suspension_section


def test_executive_summary_surfaces_ads_eligibility_count_for_suspension_findings():
    """Part 1.3: the client's stated use case is pre-ads compliance
    checking - the count of suspension-risk findings that would also block
    paid Shopping ads eligibility must be visible in the Executive Summary,
    not just per-finding detail."""
    findings = [
        _finding("llm_prohibited_content", Severity.CRITICAL, ads_eligibility_impact=AdsEligibilityImpact.ADS_AND_LISTINGS),
        _finding("business_identity_present", Severity.CRITICAL, ads_eligibility_impact=AdsEligibilityImpact.ADS_AND_LISTINGS),
    ]
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(), findings)
    exec_summary = report.split("## Executive Summary")[1].split("### At a Glance")[0]
    assert "2 of these would also affect paid Shopping ads eligibility" in exec_summary


def test_citation_without_a_verified_date_omits_it_gracefully():
    findings = [_finding(
        "llm_prohibited_content", Severity.CRITICAL,
        policy_reference='GMC policy: Prohibited content [prohibited_content] - https://support.google.com/merchants/answer/6149970 ("Prohibited content")',
        policy_requirement_text="Google does not allow ads or destinations that display shocking content.",
        policy_last_verified=None,
    )]
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(), findings, major_only=True)
    suspension_section = report.split("## Suspension Risk Findings")[1].split("## Policy-by-Policy Review")[0]
    assert "**Official Source:** https://support.google.com/merchants/answer/6149970" in suspension_section
    assert "last verified" not in suspension_section


def test_lower_priority_finding_also_gets_rag_grounded_fields_not_just_suspension_risk():
    """Follow-up round, Part 5: Finding.policy_requirement_text is set at
    grading time by any LLM-graded check regardless of which impact tier it
    is later classified into (app.impact_tier runs after grading) - the
    real store test that motivated this (meo.fr) showed llm_editorial_quality
    findings (quality_improvement tier, so rendered via _format_finding in
    "Other Findings", not _format_finding_rich in "Suspension Risk Findings")
    carrying real RAG text and a real source URL on the Finding object that
    the report was silently dropping at render time. Must not be dropped."""
    findings = [_finding(
        "llm_editorial_quality", Severity.MEDIUM,
        policy_reference='GMC policy: Editorial and professional content quality [editorial_quality] - https://support.google.com/merchants/answer/12079604 ("What you can do")',
        policy_requirement_text="Product descriptions should be free of spelling and grammatical errors.",
    )]
    assert findings[0].impact_tier == ImpactTier.QUALITY_IMPROVEMENT  # confirms this lands in Other Findings, not Suspension Risk
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(), findings)
    other_section = report.split("## Other Findings (Lower Priority)")[1]

    assert "Specific Policy Requirement" in other_section
    assert "Product descriptions should be free of spelling" in other_section
    assert "Official Source: https://support.google.com/merchants/answer/12079604" in other_section


# --- Catalog grouping (Part 2 + 3) ------------------------------------------

def test_pagination_variants_of_the_same_category_are_grouped_in_the_report():
    pages = [
        _page("https://shop.example/", PageType.HOMEPAGE),
        _page("https://shop.example/product-category/mugs", PageType.COLLECTION, depth=1),
        _page("https://shop.example/product-category/mugs/page/2", PageType.COLLECTION, depth=2),
    ]
    findings = [_finding("llm_prohibited_content", Severity.CRITICAL, page_url="https://shop.example/product-category/mugs")]
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(*pages), findings, major_only=True)

    assert "### Catalog Overview" in report
    assert "2 page variant(s) checked" in report
    # Only one grouped entry for the category, not two separate URL entries.
    assert report.count("https://shop.example/product-category/mugs") <= 3  # header + finding page ref + prose, not one per pagination page


def test_catalog_pages_with_no_findings_are_rolled_into_a_summary_not_listed_individually():
    pages = [
        _page("https://shop.example/", PageType.HOMEPAGE),
        _page("https://shop.example/product-category/mugs", PageType.COLLECTION, depth=1),
        _page("https://shop.example/product-category/hats", PageType.COLLECTION, depth=1),
    ]
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(*pages), [], major_only=True)
    catalog_section = report.split("### Catalog Overview")[1]

    assert "### [" not in catalog_section  # nothing flagged individually
    assert "2 with no findings at all" in catalog_section or "0 with a suspension-risk issue" in catalog_section


# --- API-verification-gap recommendation (Part 5) ---------------------------

def test_woocommerce_without_api_verification_gets_a_credentials_recommendation():
    findings = [_finding("required_page_present", Severity.CRITICAL)]  # page-only, no API_VERIFIED findings
    report = generate_markdown_report(_PLATFORM_WOOCOMMERCE, _site_map(), findings)
    assert "WC_CONSUMER_KEY" in report
    assert "stronger audit" in report.lower()


def test_unknown_platform_gets_no_woocommerce_recommendation():
    report = generate_markdown_report(_PLATFORM_UNKNOWN, _site_map(), [])
    assert "WC_CONSUMER_KEY" not in report
