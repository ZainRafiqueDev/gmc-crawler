"""app.checks.duplicate_products - flags the same (or near-identical)
product title appearing at more than one URL. Grounded as quality_improvement
via editorial_quality's own retrieved text, which names this exact scenario
(see app/checks/duplicate_products.py's module docstring)."""
from app.checks.duplicate_products import check_duplicate_products
from app.impact_tier import tier_for_check_id
from app.models import Confidence, CrawledPage, ImpactTier, PageType, Severity, SiteMap


def _product(url: str, title: str, heading: str | None = None) -> CrawledPage:
    return CrawledPage(
        url=url, page_type=PageType.PRODUCT, depth=1, reachable=True, title=title,
        headings=[heading] if heading else [],
    )


def _site_map(*pages: CrawledPage) -> SiteMap:
    return SiteMap(base_url="https://shop.example/", pages=list(pages))


def test_identical_title_on_two_urls_is_flagged():
    pages = [
        _product("https://shop.example/product/kayak", "Radar Fishing Kayak", heading="Radar Fishing Kayak"),
        _product("https://shop.example/product/kayak-2", "Radar Fishing Kayak", heading="Radar Fishing Kayak"),
    ]
    findings = check_duplicate_products(_site_map(*pages))
    assert len(findings) == 1
    assert findings[0].confidence == Confidence.CONFIRMED
    assert findings[0].severity == Severity.LOW
    assert "kayak" in findings[0].evidence
    assert "kayak-2" in findings[0].evidence


def test_case_and_whitespace_differences_still_count_as_identical():
    pages = [
        _product("https://shop.example/product/a", "Radar   Fishing Kayak", heading="Radar   Fishing Kayak"),
        _product("https://shop.example/product/b", "radar fishing kayak", heading="radar fishing kayak"),
    ]
    findings = check_duplicate_products(_site_map(*pages))
    assert len(findings) == 1


def test_unique_titles_produce_no_findings():
    pages = [
        _product("https://shop.example/product/a", "Radar Fishing Kayak", heading="Radar Fishing Kayak"),
        _product("https://shop.example/product/b", "Wooden Coffee Table", heading="Wooden Coffee Table"),
    ]
    findings = check_duplicate_products(_site_map(*pages))
    assert findings == []


def test_single_product_is_never_flagged():
    pages = [_product("https://shop.example/product/a", "Radar Fishing Kayak", heading="Radar Fishing Kayak")]
    assert check_duplicate_products(_site_map(*pages)) == []


def test_near_duplicate_titles_are_flagged_as_potential_risk():
    pages = [
        _product("https://shop.example/product/a", "Radar Fishing Kayak 430lbs", heading="Radar Fishing Kayak 430lbs"),
        _product("https://shop.example/product/b", "Radar Fishing Kayak 430lb", heading="Radar Fishing Kayak 430lb"),
    ]
    findings = check_duplicate_products(_site_map(*pages))
    assert len(findings) == 1
    assert findings[0].confidence == Confidence.POTENTIAL_RISK


def test_heading_preferred_over_title_which_often_has_a_site_suffix():
    # <title> commonly includes " - Site Name", which would make two
    # otherwise-identical products compare as different if used directly.
    pages = [
        _product("https://shop.example/product/a", "Radar Fishing Kayak - Shop Example", heading="Radar Fishing Kayak"),
        _product("https://shop.example/product/b", "Radar Fishing Kayak - Shop Example", heading="Radar Fishing Kayak"),
    ]
    findings = check_duplicate_products(_site_map(*pages))
    assert len(findings) == 1


def test_non_product_pages_are_ignored():
    pages = [
        CrawledPage(url="https://shop.example/collections/kayaks", page_type=PageType.COLLECTION, depth=1, reachable=True, title="Kayaks", headings=["Kayaks"]),
        CrawledPage(url="https://shop.example/collections/kayaks-2", page_type=PageType.COLLECTION, depth=1, reachable=True, title="Kayaks", headings=["Kayaks"]),
    ]
    assert check_duplicate_products(_site_map(*pages)) == []


def test_unreachable_products_are_not_compared():
    pages = [
        _product("https://shop.example/product/a", "Radar Fishing Kayak", heading="Radar Fishing Kayak"),
        CrawledPage(url="https://shop.example/product/b", page_type=PageType.PRODUCT, depth=1, reachable=False, title="Radar Fishing Kayak", headings=["Radar Fishing Kayak"]),
    ]
    assert check_duplicate_products(_site_map(*pages)) == []


def test_tier_is_quality_improvement_grounded_via_editorial_quality():
    assert tier_for_check_id("duplicate_product_listing") == ImpactTier.QUALITY_IMPROVEMENT
