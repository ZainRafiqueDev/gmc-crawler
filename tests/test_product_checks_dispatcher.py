"""Tests for the platform-advisory product check dispatcher (Goal 1):
nothing downstream should ever refuse to run because the platform is
unrecognized or its API is unreachable - it should always fall back to the
generic page-only check instead.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.checks.product_checks import run_product_checks
from app.config import Settings
from app.models import CrawledPage, Platform, PlatformDetectionResult, PageType, SiteMap, VerificationMethod

WC_PRODUCT_HTML = '<html><body><p class="price"><span class="amount">$19.99</span></p><p class="stock in-stock">In stock</p></body></html>'
SHOPIFY_PRODUCT_HTML = '<html><body><div class="price">$19.99</div><button>Add to cart</button></body></html>'
GENERIC_PRODUCT_HTML_GOOD = "<html><body>Widget - $19.99. In stock. Add to cart.</body></html>"
GENERIC_PRODUCT_HTML_NO_PRICE = "<html><body>Widget. Add to cart.</body></html>"


def _wc_page(url: str, html: str) -> CrawledPage:
    return CrawledPage(url=url, page_type=PageType.PRODUCT, depth=1, reachable=True, html=html, text=html)


def _generic_page(url: str, text: str) -> CrawledPage:
    return CrawledPage(url=url, page_type=PageType.PRODUCT, depth=1, reachable=True, html=f"<html><body>{text}</body></html>", text=text)


@pytest.mark.asyncio
async def test_unknown_platform_runs_generic_checks_never_hard_fails():
    page = _generic_page("https://custom.example/products/widget", GENERIC_PRODUCT_HTML_NO_PRICE)
    site_map = SiteMap(base_url="https://custom.example/", pages=[page])
    platform = PlatformDetectionResult(platform=Platform.UNKNOWN, base_url="https://custom.example/", evidence=["no signal"])
    settings = Settings()

    findings, _ = await run_product_checks(site_map, platform, settings)
    assert any(f.check_id == "generic_product_price_missing" for f in findings)
    assert all(f.verification_method == VerificationMethod.PAGE_ONLY for f in findings)


@pytest.mark.asyncio
async def test_wordpress_without_woocommerce_runs_generic_checks():
    page = _generic_page("https://blog.example/products/widget", GENERIC_PRODUCT_HTML_GOOD)
    site_map = SiteMap(base_url="https://blog.example/", pages=[page])
    platform = PlatformDetectionResult(platform=Platform.WORDPRESS, base_url="https://blog.example/", evidence=[])
    settings = Settings()

    findings, _ = await run_product_checks(site_map, platform, settings)
    assert findings == []  # page has price + availability signal, generic check passes


@pytest.mark.asyncio
async def test_woocommerce_without_credentials_falls_back_to_generic():
    page = _wc_page("https://shop.example/product/widget", WC_PRODUCT_HTML)
    site_map = SiteMap(base_url="https://shop.example/", pages=[page])
    platform = PlatformDetectionResult(platform=Platform.WOOCOMMERCE, base_url="https://shop.example/", evidence=[])
    settings = Settings(wc_consumer_key="", wc_consumer_secret="")

    with patch("app.checks.product_checks.fetch_wc_products", new_callable=AsyncMock) as mock_fetch:
        findings, _ = await run_product_checks(site_map, platform, settings)
        mock_fetch.assert_not_called()
    # page has price ($19.99) and availability ("in stock" in html/text) - generic check passes clean
    assert findings == []


@pytest.mark.asyncio
async def test_woocommerce_with_credentials_uses_api_verified_path():
    page = _wc_page("https://shop.example/product/widget", WC_PRODUCT_HTML)
    site_map = SiteMap(base_url="https://shop.example/", pages=[page])
    platform = PlatformDetectionResult(platform=Platform.WOOCOMMERCE, base_url="https://shop.example/", evidence=[])
    settings = Settings(wc_consumer_key="ck_x", wc_consumer_secret="cs_x")
    api_products = [{"permalink": "https://shop.example/product/widget", "price": "24.99", "stock_status": "instock"}]

    with patch("app.checks.product_checks.fetch_wc_products", new_callable=AsyncMock, return_value=api_products):
        findings, _ = await run_product_checks(site_map, platform, settings)

    assert len(findings) == 1
    assert findings[0].check_id == "woocommerce_price_mismatch"
    assert findings[0].verification_method == VerificationMethod.API_VERIFIED


@pytest.mark.asyncio
async def test_woocommerce_api_returns_nothing_falls_back_to_generic_for_all_pages():
    page = _generic_page("https://shop.example/product/widget", GENERIC_PRODUCT_HTML_NO_PRICE)
    site_map = SiteMap(base_url="https://shop.example/", pages=[page])
    platform = PlatformDetectionResult(platform=Platform.WOOCOMMERCE, base_url="https://shop.example/", evidence=[])
    settings = Settings(wc_consumer_key="ck_x", wc_consumer_secret="cs_x")

    with patch("app.checks.product_checks.fetch_wc_products", new_callable=AsyncMock, return_value=[]):
        findings, _ = await run_product_checks(site_map, platform, settings)

    assert any(f.check_id == "generic_product_price_missing" for f in findings)
    assert all(f.verification_method == VerificationMethod.PAGE_ONLY for f in findings)


@pytest.mark.asyncio
async def test_shopify_needs_no_credentials_and_uses_api_verified_path():
    page = _wc_page("https://shop.example/products/widget", SHOPIFY_PRODUCT_HTML)
    site_map = SiteMap(base_url="https://shop.example/", pages=[page])
    platform = PlatformDetectionResult(platform=Platform.SHOPIFY, base_url="https://shop.example/", evidence=[])
    settings = Settings()
    api_products = [{"handle": "widget", "variants": [{"price": "24.99", "available": True}]}]

    with patch("app.checks.product_checks.fetch_shopify_products", new_callable=AsyncMock, return_value=api_products):
        findings, _ = await run_product_checks(site_map, platform, settings)

    assert len(findings) == 1
    assert findings[0].check_id == "shopify_price_mismatch"
    assert findings[0].verification_method == VerificationMethod.API_VERIFIED


@pytest.mark.asyncio
async def test_shopify_products_json_blocked_falls_back_to_generic():
    page = _generic_page("https://shop.example/products/widget", GENERIC_PRODUCT_HTML_GOOD)
    site_map = SiteMap(base_url="https://shop.example/", pages=[page])
    platform = PlatformDetectionResult(platform=Platform.SHOPIFY, base_url="https://shop.example/", evidence=[])
    settings = Settings()

    with patch("app.checks.product_checks.fetch_shopify_products", new_callable=AsyncMock, return_value=[]):
        findings, _ = await run_product_checks(site_map, platform, settings)

    assert findings == []  # page-only check passes clean


@pytest.mark.asyncio
async def test_no_product_pages_returns_empty_without_any_api_calls():
    site_map = SiteMap(base_url="https://shop.example/", pages=[])
    platform = PlatformDetectionResult(platform=Platform.SHOPIFY, base_url="https://shop.example/", evidence=[])
    settings = Settings()

    with patch("app.checks.product_checks.fetch_shopify_products", new_callable=AsyncMock) as mock_fetch:
        findings, _ = await run_product_checks(site_map, platform, settings)
        mock_fetch.assert_not_called()
    assert findings == []
