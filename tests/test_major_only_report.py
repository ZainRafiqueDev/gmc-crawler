"""major_only=True on generate_markdown_report/generate_delta_report: only
suspension-risk findings (Critical severity, or any severity whose check is
policy-grounded as GMC-suspension-tied - see app/impact_tier.py) are
itemized; everything else is hidden but counted, not silently dropped."""
from app.impact_tier import tier_for_check_id
from app.models import Confidence, CrawledPage, Finding, PageType, Platform, PlatformDetectionResult, Severity, SiteMap
from app.report import generate_delta_report, generate_markdown_report

_PLATFORM = PlatformDetectionResult(platform=Platform.UNKNOWN, base_url="https://shop.example/", evidence=[])


def _finding(
    check_id: str, severity: Severity, page_url: str = "https://shop.example/",
    confidence: Confidence = Confidence.CONFIRMED, recommended_fix: str = "fix it",
) -> Finding:
    # impact_tier is populated here the same way the real pipeline populates
    # it (app.graph._llm_grading_node calling apply_impact_tiers) - a bare
    # Finding(...) always defaults to LISTING_DISAPPROVAL regardless of
    # check_id, so tests must set it explicitly to match real report input.
    return Finding(
        check_id=check_id, title=f"title-{check_id}", severity=severity, confidence=confidence,
        page_url=page_url, evidence="evidence text", impact_tier=tier_for_check_id(check_id),
        recommended_fix=recommended_fix,
    )


def _site_map(url: str = "https://shop.example/") -> SiteMap:
    page = CrawledPage(url=url, page_type=PageType.HOMEPAGE, depth=0, reachable=True)
    return SiteMap(base_url=url, pages=[page])


def test_major_only_hides_non_suspension_findings():
    findings = [
        _finding("business_identity_present", Severity.CRITICAL),  # critical severity -> shown
        _finding("product_image_missing_alt_text", Severity.LOW),  # listing_disapproval -> hidden
        _finding("broken_internal_link", Severity.MEDIUM),  # ambiguous/listing_disapproval -> hidden
    ]
    report = generate_markdown_report(_PLATFORM, _site_map(), findings, major_only=True)

    assert "title-business_identity_present" in report
    assert "title-product_image_missing_alt_text" not in report
    assert "title-broken_internal_link" not in report
    assert "Showing suspension-risk issues only" in report
    assert "2 lower-priority finding(s) not shown" in report


def test_major_only_includes_suspension_tier_regardless_of_severity():
    """The redefinition's whole point: severity alone no longer decides -
    a HIGH-severity listing_disapproval finding is excluded from the
    Suspension Risk Findings section, while a MEDIUM-severity
    suspension_risk-tier finding (business identity inconsistency) is
    included, because it's tier that now drives visibility, not severity.
    (The Policy-by-Policy Review matrix summarizes every finding regardless
    of tier by design, so it's excluded from this section-scoped check.)
    """
    findings = [
        _finding("woocommerce_price_mismatch", Severity.HIGH),  # listing_disapproval despite HIGH -> hidden
        _finding("business_identity_phone_consistency", Severity.MEDIUM),  # suspension_risk despite MEDIUM -> shown
    ]
    report = generate_markdown_report(_PLATFORM, _site_map(), findings, major_only=True)
    suspension_section = report.split("## Suspension Risk Findings")[1].split("## Policy-by-Policy Review")[0]

    assert "title-woocommerce_price_mismatch" not in suspension_section
    assert "title-business_identity_phone_consistency" in suspension_section


def test_major_only_excludes_cannot_verify_even_if_suspension_tier():
    findings = [_finding("llm_prohibited_content", Severity.CRITICAL, confidence=Confidence.CANNOT_VERIFY)]
    report = generate_markdown_report(_PLATFORM, _site_map(), findings, major_only=True)

    assert "title-llm_prohibited_content" not in report
    assert "0 issue(s) found that could put this store" in report


def test_full_report_shows_everything_by_default():
    findings = [
        _finding("business_identity_present", Severity.CRITICAL),
        _finding("product_image_missing_alt_text", Severity.LOW),
    ]
    report = generate_markdown_report(_PLATFORM, _site_map(), findings)

    assert "title-business_identity_present" in report
    assert "title-product_image_missing_alt_text" in report
    assert "Showing suspension-risk issues only" not in report
    assert "## Other Findings (Lower Priority)" in report


def test_major_only_page_with_no_suspension_findings_reads_as_such():
    findings = [_finding("product_image_missing_alt_text", Severity.LOW)]
    report = generate_markdown_report(_PLATFORM, _site_map(), findings, major_only=True)

    assert "No suspension-risk issues found." in report


def test_required_fixes_never_contradicts_page_by_page_suspension_status():
    """Regression: a real report showed a page as [PASS] "no suspension-risk
    issues found" in the page-by-page section while Required Fixes
    separately listed a HIGH-severity, non-suspension-tier finding for that
    same page - Required Fixes used to include any Critical/High finding
    regardless of tier, a leftover from the pre-tier design. Both sections
    must now draw from exactly the same suspension-risk predicate.
    """
    # A genuinely non-suspension-tier HIGH-severity finding.
    findings = [_finding("woocommerce_price_mismatch", Severity.HIGH)]
    report = generate_markdown_report(_PLATFORM, _site_map(), findings, major_only=True)

    assert "No suspension-risk issues found." in report
    assert "title-woocommerce_price_mismatch" not in report.split("## Required Fixes")[1]


def test_llm_policy_substance_finding_is_consistent_across_sections():
    """The real bug: llm_policy_substance_* check_ids were unclassified
    (defaulted to listing_disapproval) despite being HIGH severity, so they
    showed in Required Fixes but not in the suspension-risk page-by-page
    view. Now grounded as suspension_risk (app/impact_tier.py) - both
    sections must agree.
    """
    findings = [_finding("llm_policy_substance_privacy_policy", Severity.HIGH)]
    report = generate_markdown_report(_PLATFORM, _site_map(), findings, major_only=True)

    assert "title-llm_policy_substance_privacy_policy" in report
    assert "No suspension-risk issues found." not in report
    required_fixes_section = report.split("## Required Fixes")[1]
    assert "[GMC suspension risk] fix it" in required_fixes_section


def test_major_only_delta_report_hides_non_suspension_new_and_resolved_issues():
    previous = [
        _finding("required_page_present", Severity.CRITICAL),
        _finding("product_image_missing_alt_text", Severity.LOW),
    ]
    current = [
        _finding("business_identity_phone_consistency", Severity.HIGH),
        _finding("broken_internal_link", Severity.MEDIUM),
    ]
    report = generate_delta_report(_PLATFORM, _site_map(), previous, current, major_only=True)

    assert "title-business_identity_phone_consistency" in report  # new + suspension_risk tier -> shown
    assert "title-broken_internal_link" not in report  # new + listing_disapproval -> hidden
    assert "title-required_page_present" in report  # resolved + was critical -> shown
    assert "title-product_image_missing_alt_text" not in report  # resolved + was listing_disapproval -> hidden
    assert "Showing suspension-risk issues only" in report
