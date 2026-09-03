import httpx
import pytest
import respx

from app.checks.business_identity import check_business_identity_consistency
from app.checks.deterministic import (
    check_broken_images,
    check_broken_internal_links,
    check_duplicate_nav_footer,
    check_external_links,
    check_https,
    check_required_pages,
)
from app.models import Confidence, CrawledPage, PageType, Severity, SiteMap


# --- Failure-reporting specificity (follow-up round, Part 1.3) -------------

@pytest.mark.asyncio
@respx.mock
async def test_broken_image_network_failure_names_a_specific_category():
    """A network-level probe failure must name a real failure category
    (e.g. "network error") with an accurate recommendation, not a raw
    exception repr paired with a guessed "may be blocking automated
    requests" recommendation that's wrong for a plain timeout."""
    page = CrawledPage(
        url="https://shop.example/products/widget", page_type=PageType.PRODUCT, depth=1, reachable=True,
        image_srcs=["https://shop.example/img/widget.jpg"],
    )
    respx.head("https://shop.example/img/widget.jpg").mock(side_effect=httpx.ConnectTimeout("timed out"))
    respx.get("https://shop.example/img/widget.jpg").mock(side_effect=httpx.ConnectTimeout("timed out"))
    site_map = SiteMap(base_url="https://shop.example/", pages=[page])

    findings = await check_broken_images(site_map)

    assert len(findings) == 1
    assert findings[0].confidence == Confidence.CANNOT_VERIFY
    assert "network error" in findings[0].title.lower()
    assert "DNS/connectivity" in findings[0].recommended_fix


def test_external_links_flags_every_instance_with_exact_link_and_page(sample_site_map):
    findings = check_external_links(sample_site_map)
    evidences = {f.evidence for f in findings}

    assert any("facebook.com/acmegadgets" in e and "acmegadgets.example/" in e for e in evidences)
    assert any("instagram.com/acmegadgets" in e for e in evidences)
    assert any("totally-unrelated-blog.example" in e for e in evidences)
    # exactly the 3 external links present on the homepage, none from other pages
    assert len(findings) == 3
    assert all(f.page_url == "https://acmegadgets.example/" for f in findings)


def test_required_pages_all_present_in_sample_site(sample_site_map):
    findings = check_required_pages(sample_site_map)
    # sample site has privacy + contact but not shipping/returns/terms pages crawled
    missing_titles = {f.title for f in findings}
    assert "Missing required page: Shipping policy" in missing_titles
    assert "Missing required page: Returns/refund policy" in missing_titles
    assert "Missing required page: Terms of service" in missing_titles
    assert not any("Privacy policy" in t for t in missing_titles)
    assert not any("Contact info" in t for t in missing_titles)


def test_business_identity_flags_multiple_emails_and_phones(sample_site_map):
    findings = check_business_identity_consistency(sample_site_map)
    by_check = {f.check_id: f for f in findings}

    assert "business_identity_email_consistency" in by_check
    assert "acmegadgets.co.uk" in by_check["business_identity_email_consistency"].evidence
    assert "acme-us-office.com" in by_check["business_identity_email_consistency"].evidence

    assert "business_identity_phone_consistency" in by_check


def test_business_identity_flags_phone_country_mismatch():
    html = """
    <html><head><title>Contact</title></head><body>
    <main><h1>Contact</h1>
    <p>Call us at +91 98765 43210.</p>
    <p>Our warehouse: 100 Queen Street, Toronto, Ontario, Canada</p>
    </main></body></html>
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    page = CrawledPage(
        url="https://mismatch.example/contact-us",
        page_type=PageType.CONTACT_ABOUT,
        depth=1,
        reachable=True,
        text=soup.get_text(separator=" ", strip=True),
        html=html,
    )
    site_map = SiteMap(base_url="https://mismatch.example/", pages=[page])
    findings = check_business_identity_consistency(site_map)
    mismatch = [f for f in findings if f.check_id == "business_identity_phone_country_mismatch"]
    assert len(mismatch) == 1
    assert "canada" in mismatch[0].evidence.lower()
    assert mismatch[0].confidence == Confidence.POTENTIAL_RISK


def test_business_identity_no_finding_when_single_consistent_identity():
    html = """
    <html><body><main>
    <p>Contact: hello@onestore.com, +1 212 555 0100</p>
    <p>123 Main St, New York, NY, USA</p>
    </main></body></html>
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    page = CrawledPage(
        url="https://onestore.example/contact-us",
        page_type=PageType.CONTACT_ABOUT,
        depth=1,
        reachable=True,
        text=soup.get_text(separator=" ", strip=True),
        html=html,
    )
    site_map = SiteMap(base_url="https://onestore.example/", pages=[page])
    findings = check_business_identity_consistency(site_map)
    assert findings == []


def test_business_identity_critical_when_nothing_found():
    page = CrawledPage(
        url="https://nocontact.example/",
        page_type=PageType.HOMEPAGE,
        depth=0,
        reachable=True,
        text="Welcome to our store. Buy stuff here.",
        html="<html><body>Welcome to our store. Buy stuff here.</body></html>",
    )
    site_map = SiteMap(base_url="https://nocontact.example/", pages=[page])
    findings = check_business_identity_consistency(site_map)
    assert len(findings) == 1
    assert findings[0].severity == Severity.CRITICAL
    assert findings[0].check_id == "business_identity_present"


def test_two_footer_copies_not_flagged_as_duplicate(sample_site_map):
    # sample_site_map's /shop page has 2 identical footers - normal
    # responsive desktop/mobile theme behavior, must not be flagged.
    findings = check_duplicate_nav_footer(sample_site_map)
    assert findings == []


def test_three_or_more_footer_copies_flagged_as_duplicate():
    from tests.conftest import THREE_FOOTERS_HTML, _page
    page = _page("https://acmegadgets.example/deals", PageType.COLLECTION, THREE_FOOTERS_HTML)
    site_map = SiteMap(base_url="https://acmegadgets.example/", pages=[page])
    findings = check_duplicate_nav_footer(site_map)
    assert len(findings) == 1
    assert findings[0].page_url == "https://acmegadgets.example/deals"
    assert "footer" in findings[0].title.lower()
    assert "3" in findings[0].title


def test_https_enforced_flags_non_https_homepage():
    page = CrawledPage(url="http://insecure.example/", page_type=PageType.HOMEPAGE, depth=0, reachable=True)
    site_map = SiteMap(base_url="http://insecure.example/", pages=[page])
    findings = check_https(site_map)
    assert any(f.check_id == "https_enforced" and f.severity == Severity.CRITICAL for f in findings)


def test_https_passes_for_fully_https_site(sample_site_map):
    findings = check_https(sample_site_map)
    assert findings == []


def test_required_pages_totally_failed_crawl_gives_one_honest_finding_not_five_false_missing():
    """Regression guard for a real gap found this round: if the homepage
    itself never loads (bot-blocked/rate-limited/DNS-down) and no sitemap
    URLs were seeded either, the old code had zero information about any
    required page type and would confidently report all 5 as "Missing" -
    a false negative from an incomplete crawl, the exact failure class the
    previous round's DNS-hiccup fix was about. Now it must produce a single
    CANNOT_VERIFY finding stating why, not five CONFIRMED/CRITICAL ones."""
    homepage = CrawledPage(
        url="https://blocked.example/", page_type=PageType.HOMEPAGE, depth=0,
        reachable=False, cannot_verify=True, status=403, error="HTTP 403 (likely blocked by bot-protection or an auth wall)",
        failure_category="bot_blocked",
    )
    site_map = SiteMap(base_url="https://blocked.example/", pages=[homepage])
    findings = check_required_pages(site_map)
    assert len(findings) == 1
    assert findings[0].check_id == "crawl_incomplete"
    assert findings[0].confidence == Confidence.CANNOT_VERIFY
    assert "bot-protection" in findings[0].evidence.lower()
    assert not any(f.title.startswith("Missing required page") for f in findings)


def test_required_pages_robots_disallowed_gives_honest_finding_not_missing():
    site_map = SiteMap(base_url="https://polite.example/", pages=[], robots_disallowed=True)
    findings = check_required_pages(site_map)
    assert len(findings) == 1
    assert findings[0].check_id == "crawl_incomplete"
    assert "robots.txt" in findings[0].evidence.lower()


def test_required_pages_downgrades_to_cannot_verify_for_unsupported_language_site(sample_site_map):
    """A store whose crawled pages are genuinely in a language this audit's
    classifier has no coverage for must not produce a confident 'Missing'
    verdict for the required-page types the classifier failed to recognize -
    same principle as the crawl-failure fix, applied to a check that "can't
    read what it's looking at" rather than one that couldn't fetch at all."""
    for page in sample_site_map.pages:
        page.detected_language = "ja"  # Japanese - not in SUPPORTED_LANGUAGES this round
    findings = check_required_pages(sample_site_map)
    missing_titles = {f.title for f in findings}
    assert not any(t.startswith("Missing required page") for t in missing_titles)
    downgraded = [f for f in findings if "language" in f.title.lower()]
    assert downgraded
    assert all(f.confidence == Confidence.CANNOT_VERIFY for f in downgraded)


def test_required_pages_still_confident_when_dominant_language_is_supported(sample_site_map):
    """A plain English site (or one in a covered language) must keep the
    existing confident CONFIRMED/CRITICAL "Missing" behavior unchanged -
    the language downgrade must not blunt this check for the common case."""
    for page in sample_site_map.pages:
        page.detected_language = "en"
    findings = check_required_pages(sample_site_map)
    missing_titles = {f.title for f in findings}
    assert "Missing required page: Shipping policy" in missing_titles
    shipping = next(f for f in findings if f.title == "Missing required page: Shipping policy")
    assert shipping.confidence == Confidence.CONFIRMED
    assert shipping.severity == Severity.CRITICAL


def test_business_identity_totally_failed_crawl_is_not_a_confident_finding():
    """Same principle as check_required_pages: check_business_identity_consistency's
    "no contact identity found" verdict must not fire from a crawl that
    fetched nothing (see the module docstring's own real-bug precedent)."""
    homepage = CrawledPage(
        url="https://blocked2.example/", page_type=PageType.HOMEPAGE, depth=0,
        reachable=False, cannot_verify=True, status=429, error="HTTP 429 (rate limited)",
        failure_category="rate_limited",
    )
    site_map = SiteMap(base_url="https://blocked2.example/", pages=[homepage])
    findings = check_business_identity_consistency(site_map)
    assert len(findings) == 1
    assert findings[0].confidence == Confidence.CANNOT_VERIFY
    assert findings[0].check_id != "business_identity_present"


def test_broken_internal_links_suppresses_redundant_cannot_verify_finding_on_totally_failed_crawl():
    """Follow-up round, Part 4: a real totally-failed crawl (homepage timed
    out) produced 3 "hidden" lower-priority findings, not 2 - the third
    (this check's own per-page CANNOT_VERIFY finding for the homepage) was
    a legitimate but pure restatement of the exact same fact
    check_required_pages/check_business_identity_consistency's own
    crawl_incomplete findings already comprehensively cover. Must not fire
    when the crawl totally failed."""
    homepage = CrawledPage(
        url="https://x.example/", page_type=PageType.HOMEPAGE, depth=0,
        reachable=False, cannot_verify=True, error="timeout", failure_category="network_error",
    )
    site_map = SiteMap(base_url="https://x.example/", pages=[homepage])
    findings = check_broken_internal_links(site_map)
    assert findings == []


def test_broken_internal_links_still_reports_a_confirmed_404_even_on_a_totally_failed_crawl():
    """A confirmed 404 is a distinct, independently verified fact (not "we
    don't know") regardless of overall crawl status - must not be
    suppressed by the same guard."""
    homepage = CrawledPage(
        url="https://x.example/", page_type=PageType.HOMEPAGE, depth=0,
        reachable=False, cannot_verify=False, status=404, error="HTTP 404",
    )
    site_map = SiteMap(base_url="https://x.example/", pages=[homepage])
    findings = check_broken_internal_links(site_map)
    assert len(findings) == 1
    assert findings[0].confidence == Confidence.CONFIRMED


def test_broken_internal_links_distinguishes_confirmed_404_from_cannot_verify():
    pages = [
        CrawledPage(url="https://x.example/", page_type=PageType.HOMEPAGE, depth=0, reachable=True),
        CrawledPage(url="https://x.example/gone", page_type=PageType.BLOG_OTHER, depth=1, reachable=False, cannot_verify=False, status=404, error="HTTP 404"),
        CrawledPage(url="https://x.example/flaky", page_type=PageType.BLOG_OTHER, depth=1, reachable=False, cannot_verify=True, error="timeout"),
    ]
    site_map = SiteMap(base_url="https://x.example/", pages=pages)
    findings = check_broken_internal_links(site_map)
    assert len(findings) == 2
    confirmed = next(f for f in findings if f.confidence == Confidence.CONFIRMED)
    cannot_verify = next(f for f in findings if f.confidence == Confidence.CANNOT_VERIFY)
    assert confirmed.page_url == "https://x.example/gone"
    assert cannot_verify.page_url == "https://x.example/flaky"
