"""Platform-agnostic, page-only product checks. Used whenever a product page
can't be cross-checked against an authoritative platform API - unknown/
custom platforms, WordPress without WooCommerce, or a WooCommerce/Shopify
store whose API is unreachable/blocked/uncredentialed. Every finding here is
`verification_method=page_only`: it's inferred from the rendered page alone,
not confirmed against a second independent source.
"""
from __future__ import annotations

import re

from app.models import Confidence, CrawledPage, Finding, Severity, VerificationMethod

_PRICE_PATTERN_RE = re.compile(
    r"(?:[$£€¥]|USD|EUR|GBP)\s?\d[\d,]*(?:\.\d{2})?|\d[\d,]*(?:\.\d{2})?\s?(?:USD|EUR|GBP|\$|£|€)",
    re.IGNORECASE,
)
_AVAILABILITY_HINT_RE = re.compile(
    r"add to cart|add to bag|buy now|in stock|out of stock|sold out|available|unavailable|pre-?order",
    re.IGNORECASE,
)


def check_generic_product_pages(product_pages: list[CrawledPage]) -> list[Finding]:
    """Best-effort verification that a crawled product page actually looks
    like a functioning product page: has a visible price and an availability
    signal. No cross-check against a second source is possible here, so
    these findings are always page_only - a "pass" means "looks fine on the
    page," not "confirmed against the store's system of record."
    """
    findings: list[Finding] = []

    for page in product_pages:
        if not page.reachable or not page.text:
            continue

        if not _PRICE_PATTERN_RE.search(page.text):
            findings.append(Finding(
                check_id="generic_product_price_missing",
                title="Product page has no visible price",
                severity=Severity.MEDIUM,
                confidence=Confidence.POTENTIAL_RISK,
                page_url=page.url,
                evidence=f"No currency/price pattern found in the rendered text of {page.url}.",
                policy_reference="GMC: Product price and availability must be clearly shown",
                recommended_fix="Confirm the price renders correctly for anonymous/logged-out visitors (not hidden behind login or JS that failed to load).",
                verification_method=VerificationMethod.PAGE_ONLY,
                location="page body text (no price element found anywhere on the page)",
            ))

        if not _AVAILABILITY_HINT_RE.search(page.text):
            findings.append(Finding(
                check_id="generic_product_availability_missing",
                title="Product page has no visible availability/purchase signal",
                severity=Severity.LOW,
                confidence=Confidence.POTENTIAL_RISK,
                page_url=page.url,
                evidence=f"No add-to-cart/in-stock/out-of-stock text found in the rendered text of {page.url}.",
                policy_reference="GMC: Product availability shown must be accurate",
                recommended_fix="Confirm the page shows a clear purchase/availability call to action.",
                verification_method=VerificationMethod.PAGE_ONLY,
                location="page body text (no add-to-cart/availability element found anywhere on the page)",
            ))

    return findings
