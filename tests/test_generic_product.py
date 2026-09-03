from app.checks.generic_product import check_generic_product_pages
from app.models import Confidence, CrawledPage, PageType, Severity, VerificationMethod


def _page(url: str, text: str) -> CrawledPage:
    return CrawledPage(url=url, page_type=PageType.PRODUCT, depth=1, reachable=True, text=text, html=f"<html><body>{text}</body></html>")


def test_no_findings_when_price_and_availability_both_present():
    page = _page("https://shop.example/products/widget", "Widget - $19.99. In stock. Add to cart.")
    findings = check_generic_product_pages([page])
    assert findings == []


def test_flags_missing_price():
    page = _page("https://shop.example/products/widget", "Widget. Add to cart.")
    findings = check_generic_product_pages([page])
    assert any(f.check_id == "generic_product_price_missing" for f in findings)
    price_finding = next(f for f in findings if f.check_id == "generic_product_price_missing")
    assert price_finding.verification_method == VerificationMethod.PAGE_ONLY
    assert price_finding.confidence == Confidence.POTENTIAL_RISK


def test_flags_missing_availability_signal():
    page = _page("https://shop.example/products/widget", "Widget - $19.99.")
    findings = check_generic_product_pages([page])
    assert any(f.check_id == "generic_product_availability_missing" for f in findings)


def test_skips_unreachable_pages():
    page = CrawledPage(url="https://shop.example/products/gone", page_type=PageType.PRODUCT, depth=1, reachable=False)
    findings = check_generic_product_pages([page])
    assert findings == []
