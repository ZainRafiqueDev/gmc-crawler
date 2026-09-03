from app.checks.woocommerce_products import check_woocommerce_products
from app.models import Confidence, CrawledPage, PageType, Severity, SiteMap, VerificationMethod

PRODUCT_HTML_MATCHING = """
<html><body>
<p class="price"><span class="amount">$19.99</span></p>
<p class="stock in-stock">In stock</p>
</body></html>
"""

PRODUCT_HTML_PRICE_MISMATCH = """
<html><body>
<p class="price"><span class="amount">$24.99</span></p>
<p class="stock in-stock">In stock</p>
</body></html>
"""

PRODUCT_HTML_STOCK_MISMATCH = """
<html><body>
<p class="price"><span class="amount">$9.99</span></p>
<p class="stock out-of-stock">Out of stock</p>
</body></html>
"""


def _page(url: str, html: str) -> CrawledPage:
    return CrawledPage(url=url, page_type=PageType.PRODUCT, depth=1, reachable=True, html=html, text="")


def test_no_findings_when_price_and_stock_match():
    page = _page("https://shop.example/product/widget", PRODUCT_HTML_MATCHING)
    site_map = SiteMap(base_url="https://shop.example/", pages=[page])
    api_products = [{"permalink": "https://shop.example/product/widget", "price": "19.99", "stock_status": "instock"}]
    findings, matched = check_woocommerce_products(site_map, api_products)
    assert findings == []
    assert matched == {"https://shop.example/product/widget"}


def test_flags_price_mismatch_and_marks_api_verified():
    page = _page("https://shop.example/product/widget", PRODUCT_HTML_PRICE_MISMATCH)
    site_map = SiteMap(base_url="https://shop.example/", pages=[page])
    api_products = [{"permalink": "https://shop.example/product/widget", "price": "19.99", "stock_status": "instock"}]
    findings, matched = check_woocommerce_products(site_map, api_products)
    mismatches = [f for f in findings if f.check_id == "woocommerce_price_mismatch"]
    assert len(mismatches) == 1
    assert mismatches[0].severity == Severity.HIGH
    assert mismatches[0].confidence == Confidence.CONFIRMED
    assert mismatches[0].verification_method == VerificationMethod.API_VERIFIED
    assert "19.99" in mismatches[0].evidence and "24.99" in mismatches[0].evidence


def test_flags_stock_mismatch():
    page = _page("https://shop.example/product/widget", PRODUCT_HTML_STOCK_MISMATCH)
    site_map = SiteMap(base_url="https://shop.example/", pages=[page])
    api_products = [{"permalink": "https://shop.example/product/widget", "price": "9.99", "stock_status": "instock"}]
    findings, _ = check_woocommerce_products(site_map, api_products)
    mismatches = [f for f in findings if f.check_id == "woocommerce_stock_mismatch"]
    assert len(mismatches) == 1
    assert "instock" in mismatches[0].evidence and "outofstock" in mismatches[0].evidence


def test_no_api_products_returns_no_findings_and_no_matches():
    page = _page("https://shop.example/product/widget", PRODUCT_HTML_MATCHING)
    site_map = SiteMap(base_url="https://shop.example/", pages=[page])
    findings, matched = check_woocommerce_products(site_map, api_products=[])
    assert findings == []
    assert matched == set()


def test_unmatched_product_page_is_not_in_matched_set():
    page = _page("https://shop.example/product/orphan", PRODUCT_HTML_MATCHING)
    site_map = SiteMap(base_url="https://shop.example/", pages=[page])
    api_products = [{"permalink": "https://shop.example/product/other-item", "price": "1.00", "stock_status": "instock"}]
    findings, matched = check_woocommerce_products(site_map, api_products)
    assert matched == set()
    assert findings == []
