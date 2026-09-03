"""Shopify-specific check: cross-check displayed price/availability on the
rendered product page against the public Shopify Storefront `/products.json`
endpoint. Unlike WooCommerce, no credentials are required - most Shopify
storefronts expose this publicly - but it can be blocked (password-protected
store, or a theme/app that disables it), in which case the caller falls back
to the generic page-only product check instead of failing.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from app.checks.product_images import ProductImage
from app.models import Confidence, CrawledPage, Finding, PageType, Severity, SiteMap, VerificationMethod
from app.security.ssrf_guard import safe_async_client

logger = logging.getLogger("gmc_audit.checks.shopify_products")

_PRICE_NUMBER_RE = re.compile(r"[\d.,]+")


async def fetch_shopify_products(base_url: str, max_pages: int = 5, per_page: int = 250) -> list[dict]:
    products: list[dict] = []
    async with safe_async_client(timeout=15.0) as client:
        for page in range(1, max_pages + 1):
            try:
                resp = await client.get(
                    f"{base_url.rstrip('/')}/products.json",
                    params={"limit": per_page, "page": page},
                )
            except httpx.HTTPError as exc:
                logger.warning("Shopify products.json request failed: %s", exc)
                break
            if resp.status_code != 200:
                logger.warning("Shopify products.json returned %d on page %d", resp.status_code, page)
                break
            try:
                body = resp.json()
            except ValueError:
                break
            batch = body.get("products") if isinstance(body, dict) else None
            if not isinstance(batch, list) or not batch:
                break
            products.extend(batch)
            if len(batch) < per_page:
                break
    return products


def _normalize_price(raw: str) -> float | None:
    match = _PRICE_NUMBER_RE.search(raw.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _extract_rendered_price(html: str) -> float | None:
    soup = BeautifulSoup(html, "lxml")
    price_el = (
        soup.select_one("[data-product-price], .product-price, .price__current, .price-item--sale, .price-item--regular, .price")
    )
    if not price_el:
        return None
    return _normalize_price(price_el.get_text())


def _extract_rendered_availability(html: str) -> bool | None:
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(separator=" ", strip=True).lower()
    if "sold out" in text or "out of stock" in text:
        return False
    if "add to cart" in text or "add to bag" in text or "buy now" in text or "in stock" in text:
        return True
    return None


def extract_images_from_api(site_map: SiteMap, api_products: list[dict]) -> dict[str, list[ProductImage]]:
    """Product images from Shopify's own structured data - api_verified,
    since it's the store's system of record, not inferred from the page.
    """
    product_pages = site_map.pages_of_type(PageType.PRODUCT)
    result: dict[str, list[ProductImage]] = {}
    for product in api_products:
        page = _match_page_for_product(product, product_pages)
        if page is None:
            continue
        images = product.get("images") or []
        result[page.url] = [
            ProductImage(url=img["src"], alt_text=(img.get("alt") or None), source=VerificationMethod.API_VERIFIED)
            for img in images if img.get("src")
        ]
    return result


def _match_page_for_product(product: dict, pages: list[CrawledPage]) -> CrawledPage | None:
    handle = product.get("handle", "")
    if not handle:
        return None
    for page in pages:
        if page.page_type != PageType.PRODUCT or not page.reachable:
            continue
        path_segments = [s for s in urlparse(page.url).path.split("/") if s]
        if path_segments and path_segments[-1] == handle:
            return page
    return None


def check_shopify_products(site_map: SiteMap, api_products: list[dict]) -> tuple[list[Finding], set[str]]:
    """Returns (findings, matched_page_urls) - matched_page_urls lets the
    caller run the generic page-only check only on the pages that couldn't
    be cross-checked against the API, instead of skipping them entirely.
    """
    findings: list[Finding] = []
    product_pages = site_map.pages_of_type(PageType.PRODUCT)
    matched_page_urls: set[str] = set()

    for product in api_products:
        page = _match_page_for_product(product, product_pages)
        if page is None or not page.html:
            continue
        matched_page_urls.add(page.url)

        variants = product.get("variants") or []
        api_prices = [_normalize_price(str(v.get("price", ""))) for v in variants]
        api_prices = [p for p in api_prices if p is not None]
        api_available = any(v.get("available") for v in variants) if variants else None

        rendered_price = _extract_rendered_price(page.html)
        if api_prices and rendered_price is not None and not any(abs(rendered_price - p) <= 0.01 for p in api_prices):
            findings.append(Finding(
                check_id="shopify_price_mismatch",
                title="Displayed price does not match Shopify product data",
                severity=Severity.HIGH,
                confidence=Confidence.CONFIRMED,
                page_url=page.url,
                evidence=f"products.json reports price(s)={api_prices}, rendered page shows {rendered_price} on {page.url}",
                policy_reference="GMC: Product price shown must match the price at checkout",
                recommended_fix="Investigate caching/currency-conversion or template bug causing the displayed price to drift from the store price.",
                verification_method=VerificationMethod.API_VERIFIED,
                location="[data-product-price], .product-price, .price (rendered product price element)",
            ))

        rendered_available = _extract_rendered_availability(page.html)
        if api_available is not None and rendered_available is not None and api_available != rendered_available:
            findings.append(Finding(
                check_id="shopify_availability_mismatch",
                title="Displayed availability does not match Shopify product data",
                severity=Severity.HIGH,
                confidence=Confidence.CONFIRMED,
                page_url=page.url,
                evidence=f"products.json reports available={api_available}, rendered page indicates available={rendered_available} on {page.url}",
                policy_reference="GMC: Product availability shown must be accurate",
                recommended_fix="Check for caching (page/CDN) serving a stale availability status.",
                verification_method=VerificationMethod.API_VERIFIED,
                location="product availability/add-to-cart text on the page",
            ))

    return findings, matched_page_urls
