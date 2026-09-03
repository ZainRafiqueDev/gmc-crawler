"""Platform-advisory product check dispatcher (Goal 1): picks the best
available verification path for product pages and never refuses to check
just because the platform is unrecognized or its API is unreachable.

- WooCommerce with credentials configured -> WC REST API cross-check for
  matched pages, generic page-only check for the rest.
- Shopify (works with no credentials - /products.json is public on most
  storefronts) -> Shopify products.json cross-check for matched pages,
  generic page-only check for the rest.
- Everything else (WordPress without WooCommerce, unknown/custom platform,
  or a platform-specific API that's blocked/uncredentialed) -> generic
  page-only check for every product page.

Also gathers per-page product images (Phase B) from the same API response
already fetched here - no duplicate network calls - and runs the
deterministic image checks. The gathered image map is returned so the LLM
vision check (app/llm/image_checks.py) can reuse it without re-fetching.

This is the single place that decides API-verified vs page-only, so the
report can honestly label which is which.
"""
from __future__ import annotations

import logging

from app.checks.generic_product import check_generic_product_pages
from app.checks.product_images import ProductImage, images_from_page_html, run_deterministic_image_checks
from app.checks.shopify_products import check_shopify_products, fetch_shopify_products
from app.checks.shopify_products import extract_images_from_api as extract_shopify_images
from app.checks.woocommerce_products import check_woocommerce_products, fetch_wc_products
from app.checks.woocommerce_products import extract_images_from_api as extract_wc_images
from app.config import Settings
from app.models import Finding, PageType, Platform, PlatformDetectionResult, SiteMap

logger = logging.getLogger("gmc_audit.checks.product_checks")


async def run_product_checks(
    site_map: SiteMap, platform: PlatformDetectionResult, settings: Settings,
) -> tuple[list[Finding], dict[str, list[ProductImage]]]:
    """Returns (findings, page_images) - page_images maps product page URL
    to the images resolved for it, for the LLM vision check to reuse.
    """
    product_pages = site_map.pages_of_type(PageType.PRODUCT)
    if not product_pages:
        return [], {}

    findings: list[Finding] = []
    matched_page_urls: set[str] = set()
    page_images: dict[str, list[ProductImage]] = {}

    if platform.platform == Platform.WOOCOMMERCE and settings.wc_consumer_key and settings.wc_consumer_secret:
        api_products = await fetch_wc_products(platform.base_url, settings.wc_consumer_key, settings.wc_consumer_secret)
        if api_products:
            logger.info("WooCommerce API returned %d product(s) - running API-verified cross-check", len(api_products))
            wc_findings, matched_page_urls = check_woocommerce_products(site_map, api_products)
            findings.extend(wc_findings)
            page_images.update(extract_wc_images(site_map, api_products))
        else:
            logger.info("WooCommerce API configured but returned no products - falling back to page-only product checks")

    elif platform.platform == Platform.SHOPIFY:
        api_products = await fetch_shopify_products(platform.base_url)
        if api_products:
            logger.info("Shopify products.json returned %d product(s) - running API-verified cross-check", len(api_products))
            shopify_findings, matched_page_urls = check_shopify_products(site_map, api_products)
            findings.extend(shopify_findings)
            page_images.update(extract_shopify_images(site_map, api_products))
        else:
            logger.info("Shopify products.json unavailable/blocked - falling back to page-only product checks")

    unmatched_pages = [p for p in product_pages if p.url not in matched_page_urls]
    if unmatched_pages:
        findings.extend(check_generic_product_pages(unmatched_pages))
        for page in unmatched_pages:
            page_images.setdefault(page.url, images_from_page_html(page))

    findings.extend(await run_deterministic_image_checks(page_images))

    return findings, page_images
