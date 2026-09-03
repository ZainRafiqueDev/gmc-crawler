"""Phase B: product image checks. Deterministic (resolution, broken links,
missing alt text, placeholder filenames) plus an LLM vision-graded check
(image/description mismatch, potential prohibited-content flag) lives in
app/llm/image_checks.py - this module owns image discovery and the
non-LLM checks.

Image source matters for verification_method: an image URL pulled from a
platform API's structured product data (WooCommerce/Shopify) is
`api_verified` - it's the store's own system of record. An image scraped
from the rendered page's <img> tags is `page_only` - inferred, not
confirmed against a second source.
"""
from __future__ import annotations

import asyncio
import logging
import re
from io import BytesIO
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from PIL import Image
from pydantic import BaseModel

from app.config import HARD_MAX_IMAGE_BYTES
from app.fetch import FAILURE_CATEGORY_RECOMMENDATIONS, FAILURE_CATEGORY_SHORT_LABELS, classify_httpx_exception
from app.models import Confidence, CrawledPage, Finding, Severity, VerificationMethod
from app.security.ssrf_guard import safe_async_client

logger = logging.getLogger("gmc_audit.checks.product_images")

MIN_DIMENSION_PX = 250  # GMC's stated minimum varies by category (100-800px); 250px is a reasonable general floor
_PLACEHOLDER_FILENAME_RE = re.compile(
    r"placeholder|default[-_]?product|no[-_]?image|coming[-_]?soon|sample[-_]?image|image[-_]?not[-_]?available|missing[-_]?image",
    re.IGNORECASE,
)
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class ProductImage(BaseModel):
    url: str
    alt_text: str | None = None
    source: VerificationMethod = VerificationMethod.PAGE_ONLY


def images_from_page_html(page: CrawledPage) -> list[ProductImage]:
    """Best-effort fallback: every <img> on the page, with alt text. Can't
    reliably isolate "the product photo" from a generic page render without
    theme-specific selectors, so this includes every image found - noisier
    than the API path, but real evidence rather than nothing.
    """
    if not page.html:
        return [ProductImage(url=u, source=VerificationMethod.PAGE_ONLY) for u in page.image_srcs]

    soup = BeautifulSoup(page.html, "lxml")
    seen: set[str] = set()
    results: list[ProductImage] = []
    for img in soup.find_all("img", src=True):
        src = urljoin(page.url, img["src"].strip())
        if not src or src in seen:
            continue
        seen.add(src)
        alt = (img.get("alt") or "").strip() or None
        results.append(ProductImage(url=src, alt_text=alt, source=VerificationMethod.PAGE_ONLY))
    return results


def _check_placeholder_filename(page_url: str, img: ProductImage) -> Finding | None:
    if not _PLACEHOLDER_FILENAME_RE.search(img.url):
        return None
    return Finding(
        check_id="product_image_placeholder_filename",
        title="Product image looks like a placeholder/stock filename",
        severity=Severity.MEDIUM,
        confidence=Confidence.POTENTIAL_RISK,
        page_url=page_url,
        evidence=f"Image URL {img.url} matches a placeholder-filename pattern.",
        policy_reference="GMC: Product images must show the actual product being sold",
        recommended_fix="Replace with a real photo of the actual product.",
        verification_method=img.source,
        location=f'img[src="{img.url}"]',
    )


def _check_missing_alt_text(page_url: str, img: ProductImage) -> Finding | None:
    if img.alt_text:
        return None
    return Finding(
        check_id="product_image_missing_alt_text",
        title="Product image missing alt text",
        severity=Severity.LOW,
        confidence=Confidence.CONFIRMED,
        page_url=page_url,
        evidence=f"Image {img.url} has no alt text.",
        policy_reference="GMC: Product images should be accessible and descriptive",
        recommended_fix="Add descriptive alt text naming the product.",
        verification_method=img.source,
        location=f'img[src="{img.url}"]',
    )


async def _probe_image(
    client: httpx.AsyncClient, url: str, max_bytes: int = HARD_MAX_IMAGE_BYTES,
) -> tuple[int | None, tuple[int, int] | None, str | None, str | None]:
    """Streams the response and aborts once max_bytes is exceeded, rather
    than downloading an attacker-controlled image fully into memory first -
    a store (or a compromised one) could otherwise serve a multi-GB "image"
    to exhaust memory/bandwidth on every audit that touches it.

    Fourth element is a failure_category (app.fetch.FAILURE_CATEGORY_LABELS
    key) - only ever set on the network-exception path below; the other
    "couldn't verify" outcomes (oversized, undecodable) already carry their
    own specific reason in the error string and aren't network failures, so
    a category would be misleading there (failure-reporting specificity
    audit, follow-up round Part 1.3 - status is None used to just mean
    "some httpx.HTTPError happened," reported with a single guessed
    recommendation that didn't fit every real cause).
    """
    try:
        async with client.stream("GET", url, timeout=_TIMEOUT, follow_redirects=True) as resp:
            if resp.status_code >= 400:
                return resp.status_code, None, None, None

            content_length = resp.headers.get("content-length")
            if content_length is not None and int(content_length) > max_bytes:
                return resp.status_code, None, f"image exceeds max size ({content_length} > {max_bytes} bytes)", None

            buf = BytesIO()
            async for chunk in resp.aiter_bytes():
                buf.write(chunk)
                if buf.tell() > max_bytes:
                    return resp.status_code, None, f"image exceeds max size (>{max_bytes} bytes), aborted download", None

            try:
                with Image.open(buf) as im:
                    return resp.status_code, im.size, None, None
            except Exception as exc:  # noqa: BLE001 - any decode failure just means "couldn't verify", not a crash
                return resp.status_code, None, f"could not decode as an image: {exc}", None
    except httpx.HTTPError as exc:
        return None, None, str(exc), classify_httpx_exception(exc)


async def run_deterministic_image_checks(page_images: dict[str, list[ProductImage]], concurrency: int = 8) -> list[Finding]:
    """page_images maps product page URL -> the images found for it (from
    whichever source - API or page HTML - the caller already resolved).
    """
    findings: list[Finding] = []

    unique_images: dict[str, tuple[str, ProductImage]] = {}
    for page_url, images in page_images.items():
        for img in images:
            unique_images.setdefault(img.url, (page_url, img))
            f = _check_placeholder_filename(page_url, img)
            if f:
                findings.append(f)
            f = _check_missing_alt_text(page_url, img)
            if f:
                findings.append(f)

    if not unique_images:
        return findings

    sem = asyncio.Semaphore(concurrency)

    async def probe_one(client: httpx.AsyncClient, url: str, page_url: str, img: ProductImage) -> None:
        async with sem:
            status, size, error, category = await _probe_image(client, url)
            if status is not None and status >= 400:
                findings.append(Finding(
                    check_id="product_image_broken",
                    title="Product image broken",
                    severity=Severity.MEDIUM,
                    confidence=Confidence.CONFIRMED,
                    page_url=page_url,
                    evidence=f"Image {url} returned HTTP {status}",
                    policy_reference="GMC: Product images must be accessible",
                    recommended_fix="Fix or replace the broken image URL.",
                    verification_method=img.source,
                    location=f'img[src="{url}"]',
                ))
            elif status is None:
                # A real network-level failure (timeout/DNS/SSRF-blocked) -
                # never "the host may be blocking automated requests" as a
                # blanket guess; the actual category names what happened.
                reason = FAILURE_CATEGORY_SHORT_LABELS.get(category or "unknown", "unknown reason")
                findings.append(Finding(
                    check_id="product_image_broken",
                    title=f"Product image could not be verified ({reason})",
                    severity=Severity.LOW,
                    confidence=Confidence.CANNOT_VERIFY,
                    page_url=page_url,
                    evidence=f"Image {url} failed to load - {reason}: {error}",
                    policy_reference="GMC: Product images must be accessible",
                    recommended_fix=FAILURE_CATEGORY_RECOMMENDATIONS.get(category or "unknown", FAILURE_CATEGORY_RECOMMENDATIONS["unknown"]),
                    verification_method=img.source,
                    location=f'img[src="{url}"]',
                ))
            elif size is None:
                # The image loaded (a real HTTP response) but couldn't be
                # decoded, or exceeded the size limit - a data-quality
                # problem with the image itself, not a reachability issue,
                # so "may be blocking automated requests" would be actively
                # wrong advice here.
                findings.append(Finding(
                    check_id="product_image_broken",
                    title="Product image could not be verified (invalid or oversized image data)",
                    severity=Severity.LOW,
                    confidence=Confidence.CANNOT_VERIFY,
                    page_url=page_url,
                    evidence=f"Image {url} loaded (HTTP {status}) but {error}",
                    policy_reference="GMC: Product images must be accessible",
                    recommended_fix="Open the image URL directly to confirm it's a valid, reasonably-sized image file.",
                    verification_method=img.source,
                    location=f'img[src="{url}"]',
                ))
            else:
                width, height = size
                if width < MIN_DIMENSION_PX or height < MIN_DIMENSION_PX:
                    findings.append(Finding(
                        check_id="product_image_low_resolution",
                        title="Product image resolution below recommended minimum",
                        severity=Severity.MEDIUM,
                        confidence=Confidence.CONFIRMED,
                        page_url=page_url,
                        evidence=f"Image {url} is {width}x{height}px (recommended minimum: {MIN_DIMENSION_PX}x{MIN_DIMENSION_PX}px)",
                        policy_reference="GMC: Product images must meet minimum resolution requirements",
                        recommended_fix="Replace with a higher-resolution image.",
                        verification_method=img.source,
                        location=f'img[src="{url}"]',
                    ))

    async with safe_async_client() as client:
        await asyncio.gather(*(probe_one(client, url, page_url, img) for url, (page_url, img) in unique_images.items()))

    return findings
