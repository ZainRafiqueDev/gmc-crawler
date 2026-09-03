"""Part 4 (broader real-world crawl robustness round): when a site can't be
meaningfully crawled, the report must state specifically why - and must
never fall back to the otherwise-default "found no issues" happy-path
language, which would misleadingly read as a clean bill of health when the
real story is "this audit never got off the ground"."""
from app.checks.business_identity import check_business_identity_consistency
from app.checks.deterministic import check_required_pages
from app.models import Confidence, CrawledPage, PageType, Platform, PlatformDetectionResult, SiteMap
from app.report import generate_markdown_report

_PLATFORM_UNKNOWN = PlatformDetectionResult(platform=Platform.UNKNOWN, base_url="https://shop.example/", evidence=[])


def _blocked_site_map(failure_category: str = "bot_blocked", status: int = 403) -> SiteMap:
    homepage = CrawledPage(
        url="https://shop.example/", page_type=PageType.HOMEPAGE, depth=0,
        reachable=False, cannot_verify=True, status=status,
        error=f"HTTP {status} (likely blocked by bot-protection or an auth wall)",
        failure_category=failure_category,
    )
    return SiteMap(base_url="https://shop.example/", pages=[homepage])


def test_report_shows_prominent_banner_when_crawl_totally_failed():
    site_map = _blocked_site_map()
    findings = check_required_pages(site_map) + check_business_identity_consistency(site_map)
    report = generate_markdown_report(_PLATFORM_UNKNOWN, site_map, findings)
    assert "This Audit Could Not Run" in report
    assert "bot-protection" in report.lower()
    assert "allowlist" in report.lower()


def test_report_banner_shown_even_under_major_only():
    """A total crawl failure is not a lower-priority finding to hide -
    major_only must not suppress the "could not run" banner."""
    site_map = _blocked_site_map()
    findings = check_required_pages(site_map) + check_business_identity_consistency(site_map)
    report = generate_markdown_report(_PLATFORM_UNKNOWN, site_map, findings, major_only=True)
    assert "This Audit Could Not Run" in report


def test_report_does_not_use_happy_path_language_when_crawl_totally_failed():
    site_map = _blocked_site_map()
    findings = check_required_pages(site_map) + check_business_identity_consistency(site_map)
    report = generate_markdown_report(_PLATFORM_UNKNOWN, site_map, findings)
    assert "found no suspension-risk or lower-tier findings at all" not in report


def test_report_distinguishes_rate_limited_from_bot_blocked_wording():
    site_map = _blocked_site_map(failure_category="rate_limited", status=429)
    findings = check_required_pages(site_map) + check_business_identity_consistency(site_map)
    report = generate_markdown_report(_PLATFORM_UNKNOWN, site_map, findings)
    assert "rate-limited" in report.lower()


def test_report_states_robots_txt_refusal_distinctly_from_a_failed_fetch():
    site_map = SiteMap(base_url="https://polite.example/", pages=[], robots_disallowed=True)
    findings = check_required_pages(site_map)
    report = generate_markdown_report(_PLATFORM_UNKNOWN, site_map, findings)
    assert "This Audit Could Not Run" in report
    assert "robots.txt" in report.lower()


def test_normal_successful_crawl_shows_no_failure_banner():
    site_map = SiteMap(base_url="https://shop.example/", pages=[
        CrawledPage(url="https://shop.example/", page_type=PageType.HOMEPAGE, depth=0, reachable=True),
    ])
    report = generate_markdown_report(_PLATFORM_UNKNOWN, site_map, [])
    assert "This Audit Could Not Run" not in report


def test_partial_cannot_verify_pages_get_a_category_breakdown_not_a_bare_count():
    site_map = SiteMap(base_url="https://shop.example/", pages=[
        CrawledPage(url="https://shop.example/", page_type=PageType.HOMEPAGE, depth=0, reachable=True),
        CrawledPage(
            url="https://shop.example/shipping-policy", page_type=PageType.SHIPPING_POLICY, depth=1,
            reachable=False, cannot_verify=True, status=429, error="HTTP 429 (rate limited)",
            failure_category="rate_limited",
        ),
        CrawledPage(
            url="https://shop.example/returns-policy", page_type=PageType.RETURNS_POLICY, depth=1,
            reachable=False, cannot_verify=True, status=403, error="HTTP 403",
            failure_category="bot_blocked",
        ),
    ])
    report = generate_markdown_report(_PLATFORM_UNKNOWN, site_map, [])
    assert "Could-not-verify breakdown" in report
    assert "1 rate-limited" in report
    assert "1 bot-blocked" in report


def test_no_numeric_score_or_risk_rating_word_anywhere_when_crawl_totally_failed():
    """Follow-up round: the At a Glance section was the one remaining
    display site still showing a confident "Internal Audit Score: 100/100
    (LOW risk)" for a totally-failed crawl, contradicting the "This Audit
    Could Not Run" banner right above it and the Final Assessment/Policy
    Matrix below it (both fixed in the previous round). Root-caused this
    time: compute_risk_score itself now returns RiskScore.not_applicable
    when the crawl totally failed, and every renderer (Executive Summary,
    At a Glance, Final Assessment) reads *that* instead of separately
    re-deriving "did the crawl fail" - this test scans the ENTIRE rendered
    report for a leaked numeric score or a bare risk-rating word, not just
    one known-bad line, so a future new display site can't reintroduce the
    same class of gap unnoticed."""
    site_map = _blocked_site_map()
    findings = check_required_pages(site_map) + check_business_identity_consistency(site_map)
    report = generate_markdown_report(_PLATFORM_UNKNOWN, site_map, findings)

    assert "/100" not in report  # no "N/100" score fragment anywhere
    for word in ("LOW risk", "MEDIUM risk", "HIGH risk"):
        assert word not in report
    assert "not applicable" in report  # the score section explicitly says so
    assert "Overall risk level: not applicable" in report


def test_policy_matrix_does_not_show_pass_for_a_totally_failed_crawl():
    """Live-found gap: with nothing else in `findings` mapping to any of the
    8 policy areas, the matrix would otherwise show every area as "Pass, 0
    findings" - reading as a clean sweep, directly contradicting the "This
    Audit Could Not Run" banner above it."""
    site_map = _blocked_site_map()
    findings = check_required_pages(site_map) + check_business_identity_consistency(site_map)
    report = generate_markdown_report(_PLATFORM_UNKNOWN, site_map, findings)
    assert "| Pass |" not in report
    assert report.count("Cannot Verify") >= 8  # one per policy area, at minimum


def test_final_assessment_does_not_show_a_clean_risk_score_for_a_totally_failed_crawl():
    site_map = _blocked_site_map()
    findings = check_required_pages(site_map) + check_business_identity_consistency(site_map)
    report = generate_markdown_report(_PLATFORM_UNKNOWN, site_map, findings)
    assert "Overall risk level: not applicable" in report
    assert "LOW risk" not in report.split("## Final Assessment")[1]
    assert "100/100" not in report.split("## Final Assessment")[1]


def test_api_verification_recommendation_suppressed_for_totally_failed_crawl():
    """Live-found gap: platform detection is a separate, independent httpx
    probe that can false-positive "woocommerce" from a WAF 403'ing every
    path uniformly - showing a WooCommerce-specific recommendation right
    next to "This Audit Could Not Run" is a real inconsistency."""
    from app.models import Platform, PlatformDetectionResult
    site_map = _blocked_site_map()
    findings = check_required_pages(site_map) + check_business_identity_consistency(site_map)
    woocommerce_platform = PlatformDetectionResult(
        platform=Platform.WOOCOMMERCE, base_url="https://shop.example/",
        evidence=["/wp-json/wc/v3/products returned 403 (route exists)"],
    )
    report = generate_markdown_report(woocommerce_platform, site_map, findings)
    assert "connect the WooCommerce REST API" not in report


def test_per_page_block_shows_the_specific_failure_reason():
    site_map = SiteMap(base_url="https://shop.example/", pages=[
        CrawledPage(url="https://shop.example/", page_type=PageType.HOMEPAGE, depth=0, reachable=True),
        CrawledPage(
            url="https://shop.example/privacy-policy", page_type=PageType.PRIVACY_POLICY, depth=1,
            reachable=False, cannot_verify=True, status=403, error="HTTP 403",
            failure_category="captcha_blocked",
        ),
    ])
    report = generate_markdown_report(_PLATFORM_UNKNOWN, site_map, [])
    assert "CAPTCHA" in report
