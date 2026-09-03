from app.checks.shopify_products import check_shopify_products
from app.models import CrawledPage, PageType, Severity, SiteMap, VerificationMethod

PRODUCT_HTML_MATCHING = """
<html><body>
<div class="price">$19.99</div>
<button>Add to cart</button>
</body></html>
"""

PRODUCT_HTML_PRICE_MISMATCH = """
<html><body>
<div class="price">$24.99</div>
<button>Add to cart</button>
</body></html>
"""

PRODUCT_HTML_SOLD_OUT_BUT_API_AVAILABLE = """
<html><body>
<div class="price">$19.99</div>
<p>Sold out</p>
</body></html>
"""


def _page(url: str, html: str) -> CrawledPage:
    return CrawledPage(url=url, page_type=PageType.PRODUCT, depth=1, reachable=True, html=html, text="")


def _product(handle: str, price: str, available: bool = True) -> dict:
    return {"handle": handle, "variants": [{"price": price, "available": available}]}


def test_no_findings_when_price_and_availability_match():
    page = _page("https://shop.example/products/widget", PRODUCT_HTML_MATCHING)
    site_map = SiteMap(base_url="https://shop.example/", pages=[page])
    findings, matched = check_shopify_products(site_map, [_product("widget", "19.99")])
    assert findings == []
    assert matched == {"https://shop.example/products/widget"}


def test_flags_price_mismatch_and_marks_api_verified():
    page = _page("https://shop.example/products/widget", PRODUCT_HTML_PRICE_MISMATCH)
    site_map = SiteMap(base_url="https://shop.example/", pages=[page])
    findings, _ = check_shopify_products(site_map, [_product("widget", "19.99")])
    mismatches = [f for f in findings if f.check_id == "shopify_price_mismatch"]
    assert len(mismatches) == 1
    assert mismatches[0].severity == Severity.HIGH
    assert mismatches[0].verification_method == VerificationMethod.API_VERIFIED
    assert "19.99" in mismatches[0].evidence and "24.99" in mismatches[0].evidence


def test_flags_availability_mismatch():
    page = _page("https://shop.example/products/widget", PRODUCT_HTML_SOLD_OUT_BUT_API_AVAILABLE)
    site_map = SiteMap(base_url="https://shop.example/", pages=[page])
    findings, _ = check_shopify_products(site_map, [_product("widget", "19.99", available=True)])
    mismatches = [f for f in findings if f.check_id == "shopify_availability_mismatch"]
    assert len(mismatches) == 1
    assert "True" in mismatches[0].evidence and "False" in mismatches[0].evidence


def test_unmatched_handle_not_in_matched_set():
    page = _page("https://shop.example/products/widget", PRODUCT_HTML_MATCHING)
    site_map = SiteMap(base_url="https://shop.example/", pages=[page])
    findings, matched = check_shopify_products(site_map, [_product("other-handle", "1.00")])
    assert matched == set()
    assert findings == []
