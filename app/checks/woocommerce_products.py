"""WooCommerce-specific check: cross-check displayed price/availability on
the rendered product page against the WooCommerce REST API (if credentials
are provided). The caller (app/checks/product_checks.py) is responsible for
falling back to the generic page-only product check for any product page
this can't match/verify (no credentials configured, API unreachable, or no
matching API product) - this function only ever returns API-verified
findings plus which pages it was able to cross-check.
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

logger = logging.getLogger("gmc_audit.checks.woocommerce_products")

_PRICE_NUMBER_RE = re.compile(r"[\d.,]+")
_STOCK_KEYWORDS = {
    "instock": ["in stock", "available"],
    "outofstock": ["out of stock", "sold out", "unavailable"],
    "onbackorder": ["backorder"],
}


async def fetch_wc_product_count(base_url: str, consumer_key: str, consumer_secret: str) -> int | None:
    """Lightweight total-catalog-size signal for adaptive page-budget sizing
    (app.site_mapper) - one per_page=1 request read for the X-WP-Total
    response header, not a full product fetch (that already happens later,
    in run_product_checks, for actual price/availability verification; this
    is only for sizing the crawl budget before the crawl itself starts).
    None on any failure (unreachable, non-200, missing/unparseable header) -
    callers fall back to the sitemap-derived signal, never block the crawl
    on this.
    """
    try:
        async with safe_async_client(auth=httpx.BasicAuth(consumer_key, consumer_secret), timeout=10.0) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/wp-json/wc/v3/products", params={"per_page": 1, "page": 1})
    except httpx.HTTPError as exc:
        logger.debug("WooCommerce product-count probe failed: %s", exc)
        return None
    if resp.status_code != 200:
        return None
    total = resp.headers.get("X-WP-Total")
    if total is None:
        return None
    try:
        return int(total)
    except ValueError:
        return None


async def fetch_wc_products(base_url: str, consumer_key: str, consumer_secret: str, max_pages: int = 5, per_page: int = 50) -> list[dict]:
    products: list[dict] = []
    async with safe_async_client(auth=httpx.BasicAuth(consumer_key, consumer_secret), timeout=15.0) as client:
        for page in range(1, max_pages + 1):
            try:
                resp = await client.get(
                    f"{base_url.rstrip('/')}/wp-json/wc/v3/products",
                    params={"per_page": per_page, "page": page},
                )
            except httpx.HTTPError as exc:
                logger.warning("WooCommerce products API request failed: %s", exc)
                break
            if resp.status_code != 200:
                logger.warning("WooCommerce products API returned %d on page %d", resp.status_code, page)
                break
            batch = resp.json()
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
    price_el = soup.select_one("p.price ins .amount, p.price ins") or soup.select_one("p.price .amount") or soup.select_one("p.price")
    if not price_el:
        return None
    return _normalize_price(price_el.get_text())


def _extract_rendered_stock_status(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    stock_el = soup.select_one("p.stock, .stock")
    text = (stock_el.get_text(strip=True).lower() if stock_el else "")
    if not text:
        text = soup.get_text(separator=" ", strip=True).lower()
    for status, keywords in _STOCK_KEYWORDS.items():
        if any(k in text for k in keywords):
            return status
    return None


def extract_images_from_api(site_map: SiteMap, api_products: list[dict]) -> dict[str, list[ProductImage]]:
    """Product images from WooCommerce's own structured data - api_verified,
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
    permalink = product.get("permalink", "")
    if not permalink:
        return None
    permalink_path = urlparse(permalink).path.rstrip("/")
    for page in pages:
        if page.page_type != PageType.PRODUCT or not page.reachable:
            continue
        if urlparse(page.url).path.rstrip("/") == permalink_path:
            return page
    return None


def check_woocommerce_products(site_map: SiteMap, api_products: list[dict]) -> tuple[list[Finding], set[str]]:
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

        api_price = _normalize_price(str(product.get("price", "")))
        rendered_price = _extract_rendered_price(page.html)
        if api_price is not None and rendered_price is not None and abs(api_price - rendered_price) > 0.01:
            findings.append(Finding(
                check_id="woocommerce_price_mismatch",
                title="Displayed price does not match WooCommerce API price",
                severity=Severity.HIGH,
                confidence=Confidence.CONFIRMED,
                page_url=page.url,
                evidence=f"API reports price={api_price}, rendered page shows {rendered_price} on {page.url}",
                policy_reference="GMC: Product price shown must match the price at checkout",
                recommended_fix="Investigate caching/currency-conversion or template bug causing the displayed price to drift from the store price.",
                verification_method=VerificationMethod.API_VERIFIED,
                location="p.price (rendered product price element)",
            ))

        api_stock = product.get("stock_status")
        rendered_stock = _extract_rendered_stock_status(page.html)
        if api_stock and rendered_stock and api_stock != rendered_stock:
            findings.append(Finding(
                check_id="woocommerce_stock_mismatch",
                title="Displayed availability does not match WooCommerce API stock status",
                severity=Severity.HIGH,
                confidence=Confidence.CONFIRMED,
                page_url=page.url,
                evidence=f"API reports stock_status={api_stock!r}, rendered page shows {rendered_stock!r} on {page.url}",
                policy_reference="GMC: Product availability shown must be accurate",
                recommended_fix="Check for caching (page/CDN) serving a stale stock status.",
                verification_method=VerificationMethod.API_VERIFIED,
                location="p.stock (rendered product availability element)",
            ))

    return findings, matched_page_urls
