"""Step 1: given a URL, detect WordPress / WooCommerce / Shopify / unknown-custom.

Primary signal for WordPress/WooCommerce is the WP REST API index
(`/wp-json/`), which advertises every active namespace (`wp/v2`, `wc/v3`,
...) in one request. If that's disabled or unreachable, fall back to direct
namespace probes, then a Shopify-specific probe (`/products.json`), then
HTML markers on the homepage (meta generator tag, wp-content/wp-includes
asset paths, woocommerce body classes, Shopify CDN/theme references).

The result is advisory, never a gate: every downstream stage (crawl,
classify, deterministic checks, LLM grading) runs the same way regardless
of what's detected here. UNKNOWN just means platform-specific enhancements
(API-verified product cross-checks) fall back to page-only checks instead.
"""
from __future__ import annotations

import logging
import re

import httpx

from app.models import Platform, PlatformDetectionResult
from app.security.ssrf_guard import GMC_AUDIT_USER_AGENT, SSRFBlockedError, safe_async_client

logger = logging.getLogger("gmc_audit.platform_detector")

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_HEADERS = {"User-Agent": GMC_AUDIT_USER_AGENT}


def _normalize_base_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = f"https://{url}"
    return url.rstrip("/")


async def _get_json(client: httpx.AsyncClient, url: str) -> tuple[int | None, dict | None]:
    try:
        resp = await client.get(url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
    except SSRFBlockedError as exc:
        logger.warning("GET %s blocked by SSRF guard (e.g. a redirect to an internal address): %s", url, exc)
        return None, None
    except httpx.HTTPError as exc:
        logger.debug("GET %s failed: %s", url, exc)
        return None, None
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, None


async def detect_platform(url: str) -> PlatformDetectionResult:
    base_url = _normalize_base_url(url)
    evidence: list[str] = []

    async with safe_async_client() as client:
        # 1. REST API index — most reliable, one request tells us every namespace.
        status, body = await _get_json(client, f"{base_url}/wp-json/")
        if status == 200 and isinstance(body, dict):
            namespaces = body.get("namespaces", [])
            if any(ns.startswith("wc/") for ns in namespaces):
                evidence.append(f"/wp-json/ index lists a woocommerce namespace: {namespaces}")
                return PlatformDetectionResult(platform=Platform.WOOCOMMERCE, base_url=base_url, evidence=evidence)
            if any(ns.startswith("wp/") for ns in namespaces):
                evidence.append(f"/wp-json/ index lists wp/v2 but no wc/* namespace: {namespaces}")
                return PlatformDetectionResult(platform=Platform.WORDPRESS, base_url=base_url, evidence=evidence)
            evidence.append(f"/wp-json/ responded 200 but no wp/wc namespaces found: {namespaces}")
        elif status is not None:
            evidence.append(f"/wp-json/ returned status {status} (not a usable namespace index)")
        else:
            evidence.append("/wp-json/ was unreachable")

        # 2. Direct namespace probes — REST index can be filtered/disabled even
        # when the underlying namespace still responds.
        wc_status, _ = await _get_json(client, f"{base_url}/wp-json/wc/v3/products")
        if wc_status in (200, 401, 403):
            evidence.append(f"/wp-json/wc/v3/products returned {wc_status} (route exists)")
            return PlatformDetectionResult(platform=Platform.WOOCOMMERCE, base_url=base_url, evidence=evidence)
        elif wc_status is not None:
            evidence.append(f"/wp-json/wc/v3/products returned {wc_status}")

        wp_status, wp_body = await _get_json(client, f"{base_url}/wp-json/wp/v2/")
        if wp_status == 200 and isinstance(wp_body, dict):
            evidence.append("/wp-json/wp/v2/ responded 200 (WordPress core REST route present)")
            return PlatformDetectionResult(platform=Platform.WORDPRESS, base_url=base_url, evidence=evidence)
        elif wp_status is not None:
            evidence.append(f"/wp-json/wp/v2/ returned {wp_status}")

        # 3. Shopify probe — /products.json is public on most Shopify storefronts
        # (no auth needed) and is a decisive signal when it returns real product data.
        shopify_status, shopify_body = await _get_json(client, f"{base_url}/products.json")
        if shopify_status == 200 and isinstance(shopify_body, dict) and isinstance(shopify_body.get("products"), list):
            evidence.append(f"/products.json returned {len(shopify_body['products'])} product(s) (Shopify Storefront API)")
            return PlatformDetectionResult(platform=Platform.SHOPIFY, base_url=base_url, evidence=evidence)
        elif shopify_status is not None:
            evidence.append(f"/products.json returned {shopify_status} (not a usable Shopify product list)")

        # 4. HTML fallback — REST API/products.json unavailable, look for markup fingerprints.
        try:
            resp = await client.get(base_url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
            html = resp.text
        except SSRFBlockedError as exc:
            evidence.append(f"Homepage fetch blocked by SSRF guard: {exc}")
            html = ""
        except httpx.HTTPError as exc:
            evidence.append(f"Homepage fetch failed for HTML fallback: {exc}")
            html = ""

        if html:
            is_wp = bool(re.search(r"wp-content|wp-includes", html, re.IGNORECASE)) or bool(
                re.search(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']WordPress', html, re.IGNORECASE)
            )
            is_wc = bool(re.search(r"woocommerce", html, re.IGNORECASE))
            is_shopify = bool(re.search(r"cdn\.shopify\.com|Shopify\.theme|shopify-section|myshopify\.com", html, re.IGNORECASE))
            if is_wc:
                evidence.append("Homepage HTML contains 'woocommerce' markers (REST API likely disabled)")
                return PlatformDetectionResult(platform=Platform.WOOCOMMERCE, base_url=base_url, evidence=evidence)
            if is_wp:
                evidence.append("Homepage HTML contains wp-content/wp-includes or WordPress generator meta tag")
                return PlatformDetectionResult(platform=Platform.WORDPRESS, base_url=base_url, evidence=evidence)
            if is_shopify:
                evidence.append("Homepage HTML contains cdn.shopify.com/Shopify.theme/myshopify.com markers (products.json likely blocked or password-protected)")
                return PlatformDetectionResult(platform=Platform.SHOPIFY, base_url=base_url, evidence=evidence)
            evidence.append("Homepage HTML has no WordPress/WooCommerce/Shopify markers")

    evidence.append("No WordPress/WooCommerce/Shopify signal found via REST API or HTML - treating as unknown/custom")
    return PlatformDetectionResult(platform=Platform.UNKNOWN, base_url=base_url, evidence=evidence)
