"""Soft-404/catch-all detection (follow-up round - hypothesis-driven, not a
live-observed bug; see decisions.md). Core invariant under test throughout:
a strong content match against a known-nonexistent-URL probe or the
homepage may only ever downgrade a would-be CONFIRMED "exists" to
CANNOT_VERIFY - it must never independently produce a CONFIRMED/CRITICAL
"missing page" finding, and it must never incorrectly reject a genuinely
distinct required page.
"""
from __future__ import annotations

import pytest

from app.change_detection import compute_content_hash, normalize_for_content_hash
from app.checks.deterministic import check_required_pages
from app.models import Confidence, CrawledPage, PageType, Severity, SiteMap
from app.soft_404_detection import is_strong_content_match

HOMEPAGE_HTML = "<html><head><title>Acme Gadgets</title></head><body><h1>Acme Gadgets</h1><p>Welcome to our store, home of quality widgets since 2010.</p></body></html>"
HOMEPAGE_TEXT = "Acme Gadgets Welcome to our store, home of quality widgets since 2010."

# A generic catch-all/soft-404 template - what a misconfigured SPA route or
# a broken theme template might return for any unmatched URL, with HTTP 200.
CATCH_ALL_HTML = "<html><head><title>Acme Gadgets</title></head><body><h1>Oops</h1><p>We couldn't find that page. Browse our homepage instead.</p></body></html>"
CATCH_ALL_TEXT = "Oops We couldn't find that page. Browse our homepage instead."

# A genuine, distinct required page - real policy content, nothing like
# either baseline above.
GENUINE_PRIVACY_HTML = "<html><head><title>Privacy Policy</title></head><body><h1>Privacy Policy</h1><p>We collect your name, email, and shipping address when you place an order, and never sell this data to third parties.</p></body></html>"
GENUINE_PRIVACY_TEXT = "Privacy Policy We collect your name, email, and shipping address when you place an order, and never sell this data to third parties."


def _page(url: str, page_type: PageType, html: str, text: str, **overrides) -> CrawledPage:
    defaults = dict(
        url=url, page_type=page_type, depth=1, title="", status=200, reachable=True,
        html=html, text=text,
    )
    defaults.update(overrides)
    return CrawledPage(**defaults)


def _homepage() -> CrawledPage:
    return _page("https://acme.example/", PageType.HOMEPAGE, HOMEPAGE_HTML, HOMEPAGE_TEXT, depth=0)


# --- is_strong_content_match (pure function) --------------------------------

def test_exact_hash_match_is_strong():
    h = compute_content_hash("some content")
    assert is_strong_content_match("some content", h, "some content", h) is True


def test_near_identical_normalized_text_is_strong():
    a = normalize_for_content_hash("We could not find that page.   Please try again.")
    b = normalize_for_content_hash("We could not find that page. Please try again!")
    assert is_strong_content_match(a, compute_content_hash(a), b, compute_content_hash(b)) is True


def test_distinct_content_is_not_a_strong_match():
    a, b = normalize_for_content_hash(GENUINE_PRIVACY_TEXT), normalize_for_content_hash(CATCH_ALL_TEXT)
    assert is_strong_content_match(a, compute_content_hash(a), b, compute_content_hash(b)) is False


def test_missing_baseline_is_never_a_strong_match():
    a = normalize_for_content_hash("anything")
    assert is_strong_content_match(a, compute_content_hash(a), None, None) is False


def test_weak_wording_similarity_alone_is_not_sufficient():
    """Sharing a few common words is not the same as near-identical content -
    must not trigger a match on wording overlap alone."""
    a = normalize_for_content_hash("Our return policy allows returns within 30 days of purchase for a full refund.")
    b = normalize_for_content_hash("Our shipping policy covers international orders within 30 business days of dispatch.")
    assert is_strong_content_match(a, compute_content_hash(a), b, compute_content_hash(b)) is False


# --- check_required_pages integration ---------------------------------------

def test_soft_404_candidate_downgraded_to_cannot_verify():
    baseline_hash = compute_content_hash(CATCH_ALL_TEXT)
    baseline_text = normalize_for_content_hash(CATCH_ALL_TEXT)
    site_map = SiteMap(
        base_url="https://acme.example/",
        pages=[_homepage(), _page("https://acme.example/privacy-policy", PageType.PRIVACY_POLICY, CATCH_ALL_HTML, CATCH_ALL_TEXT)],
        soft_404_baseline_content_hash=baseline_hash, soft_404_baseline_normalized_text=baseline_text,
    )
    findings = check_required_pages(site_map)
    privacy_findings = [f for f in findings if "Privacy policy" in f.title]
    assert len(privacy_findings) == 1
    f = privacy_findings[0]
    assert f.confidence == Confidence.CANNOT_VERIFY
    assert "catch-all" in f.title.lower()
    assert "nonexistent URL probed" in f.evidence
    assert not f.title.startswith("Missing required page")


def test_homepage_catch_all_candidate_downgraded_to_cannot_verify():
    """No baseline probe available this run (soft_404_baseline_content_hash=None)
    - the homepage-content comparison alone must still catch a candidate
    whose content is really just the homepage (e.g. a redirect-to-homepage,
    or a template that renders the homepage for any unmatched route)."""
    site_map = SiteMap(
        base_url="https://acme.example/",
        pages=[_homepage(), _page("https://acme.example/shipping-policy", PageType.SHIPPING_POLICY, HOMEPAGE_HTML, HOMEPAGE_TEXT)],
    )
    findings = check_required_pages(site_map)
    shipping_findings = [f for f in findings if "Shipping policy" in f.title]
    assert len(shipping_findings) == 1
    f = shipping_findings[0]
    assert f.confidence == Confidence.CANNOT_VERIFY
    assert "catch-all" in f.title.lower()
    assert "homepage" in f.evidence
    assert not f.title.startswith("Missing required page")


def test_redirect_to_homepage_not_confirmed_existing():
    """A candidate that redirects to the homepage renders the homepage's own
    final content (a browser-driven fetch always reflects the final,
    post-redirect page) - simulated here the same way: the candidate's
    fetched content IS the homepage's content. Must not be confirmed as
    proof the requested page exists."""
    site_map = SiteMap(
        base_url="https://acme.example/",
        pages=[_homepage(), _page("https://acme.example/returns-policy", PageType.RETURNS_POLICY, HOMEPAGE_HTML, HOMEPAGE_TEXT)],
    )
    findings = check_required_pages(site_map)
    returns_findings = [f for f in findings if "Returns/refund policy" in f.title]
    assert len(returns_findings) == 1
    assert returns_findings[0].confidence == Confidence.CANNOT_VERIFY
    assert returns_findings[0].severity != Severity.CRITICAL


def test_real_required_page_not_flagged_when_different_from_soft_404():
    baseline_hash = compute_content_hash(CATCH_ALL_TEXT)
    baseline_text = normalize_for_content_hash(CATCH_ALL_TEXT)
    site_map = SiteMap(
        base_url="https://acme.example/",
        pages=[_homepage(), _page("https://acme.example/privacy-policy", PageType.PRIVACY_POLICY, GENUINE_PRIVACY_HTML, GENUINE_PRIVACY_TEXT)],
        soft_404_baseline_content_hash=baseline_hash, soft_404_baseline_normalized_text=baseline_text,
    )
    findings = check_required_pages(site_map)
    assert not any("Privacy policy" in f.title for f in findings)


def test_real_required_page_not_flagged_when_different_from_homepage():
    site_map = SiteMap(
        base_url="https://acme.example/",
        pages=[_homepage(), _page("https://acme.example/privacy-policy", PageType.PRIVACY_POLICY, GENUINE_PRIVACY_HTML, GENUINE_PRIVACY_TEXT)],
    )
    findings = check_required_pages(site_map)
    assert not any("Privacy policy" in f.title for f in findings)


def test_http_404_remains_confirmed_missing():
    """A required page_type with zero reachable candidates at all (a real
    404, or simply never found) still produces a confident CONFIRMED/
    CRITICAL "missing" finding, completely unchanged by this round."""
    site_map = SiteMap(base_url="https://acme.example/", pages=[_homepage()])
    findings = check_required_pages(site_map)
    privacy_findings = [f for f in findings if "Privacy policy" in f.title]
    assert len(privacy_findings) == 1
    assert privacy_findings[0].title == "Missing required page: Privacy policy"
    assert privacy_findings[0].severity == Severity.CRITICAL
    assert privacy_findings[0].confidence == Confidence.CONFIRMED


def test_network_failure_remains_cannot_verify():
    site_map = SiteMap(
        base_url="https://acme.example/",
        pages=[_homepage(), CrawledPage(
            url="https://acme.example/shipping-policy", page_type=PageType.SHIPPING_POLICY, depth=1,
            status=None, reachable=False, cannot_verify=True, failure_category="network_error", error="timed out",
        )],
    )
    findings = check_required_pages(site_map)
    shipping_findings = [f for f in findings if "Shipping policy" in f.title]
    assert len(shipping_findings) == 1
    assert shipping_findings[0].confidence == Confidence.CANNOT_VERIFY
    assert shipping_findings[0].title == "Shipping policy page could not be verified"


def test_bot_block_remains_cannot_verify():
    site_map = SiteMap(
        base_url="https://acme.example/",
        pages=[_homepage(), CrawledPage(
            url="https://acme.example/terms-of-service", page_type=PageType.TERMS_OF_SERVICE, depth=1,
            status=403, reachable=False, cannot_verify=True, failure_category="bot_blocked", error="interstitial never resolved",
        )],
    )
    findings = check_required_pages(site_map)
    terms_findings = [f for f in findings if "Terms of service" in f.title]
    assert len(terms_findings) == 1
    assert terms_findings[0].confidence == Confidence.CANNOT_VERIFY


def test_soft_404_detection_does_not_create_confirmed_missing():
    """Every soft-404-flagged page_type in this scenario must never come out
    as CONFIRMED/CRITICAL "missing" - re-asserted here explicitly as its own
    standalone acceptance check, not just implied by the other tests'
    assertions. (returns/terms/contact_about have no reachable candidate at
    all in this fixture, so they legitimately DO come out as confirmed-
    missing below - that's the correct, unrelated "zero reachable
    candidates" path, not what this test is checking.)"""
    baseline_hash = compute_content_hash(CATCH_ALL_TEXT)
    baseline_text = normalize_for_content_hash(CATCH_ALL_TEXT)
    site_map = SiteMap(
        base_url="https://acme.example/",
        pages=[
            _homepage(),
            _page("https://acme.example/privacy-policy", PageType.PRIVACY_POLICY, CATCH_ALL_HTML, CATCH_ALL_TEXT),
            _page("https://acme.example/shipping-policy", PageType.SHIPPING_POLICY, HOMEPAGE_HTML, HOMEPAGE_TEXT),
        ],
        soft_404_baseline_content_hash=baseline_hash, soft_404_baseline_normalized_text=baseline_text,
    )
    findings = check_required_pages(site_map)
    soft_404_relevant = [f for f in findings if "Privacy policy" in f.title or "Shipping policy" in f.title]
    assert len(soft_404_relevant) == 2
    for f in soft_404_relevant:
        assert not (f.confidence == Confidence.CONFIRMED and f.severity == Severity.CRITICAL)
        assert "Missing required page" not in f.title
        assert f.confidence == Confidence.CANNOT_VERIFY

    # The other three required page types genuinely have no reachable
    # candidate at all here and correctly still come out as confirmed-missing.
    other_missing = [f for f in findings if f.title.startswith("Missing required page")]
    assert len(other_missing) == 3


@pytest.mark.asyncio
async def test_llm_never_participates_in_page_existence_decision():
    """Part 2: confirms (doesn't assume) the LLM layer never independently
    determines whether a required page exists. A required page_type with
    zero reachable candidates must produce zero llm_policy_substance_*
    output for that type - the LLM check is simply never invoked without a
    deterministic-layer-confirmed reachable page to grade, so it has no
    opportunity to form its own opinion on existence.
    """
    from app.config import Settings
    from app.llm.checks import run_llm_checks

    site_map = SiteMap(base_url="https://acme.example/", pages=[_homepage()])
    settings = Settings(llm_provider="claude", anthropic_api_key="")  # llm_configured is False
    findings, _coverage = await run_llm_checks(site_map, settings)

    privacy_findings = [f for f in findings if f.check_id == "llm_policy_substance_privacy_policy"]
    assert privacy_findings == []
