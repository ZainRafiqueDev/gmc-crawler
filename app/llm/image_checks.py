"""Phase B: LLM vision-graded product image checks (Claude or OpenAI, per
Settings.llm_provider - see app/llm/factory.py). Images are passed by URL;
both providers fetch the image server-side, no local download needed.

Two things are checked per image:
  1. Does the image plausibly show the product described in the page's
     title/text (mismatch detection)?
  2. Does the image raise a prohibited-content concern?

The prohibited-content check is a screening flag for human review only -
per the project brief, this is never reported as a confirmed policy
violation, regardless of how confident the model's response claims to be.
"""
from __future__ import annotations

import asyncio
import logging

from app.checks.product_images import ProductImage
from app.config import Settings
from app.llm.cache import LLMCache
from app.llm.client import LLMClient
from app.llm.factory import get_llm_client
from app.llm.policy_snippets import get_snippet
from app.models import Confidence, CrawledPage, Finding, PageType, Severity, SiteMap

logger = logging.getLogger("gmc_audit.llm.image_checks")

_MAX_PRODUCT_PAGES_CHECKED = 5
_LLM_CONCURRENCY = 3
_PAGE_TEXT_LIMIT = 1500

_VISION_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "plausible_match": {"type": "boolean", "description": "Does the image plausibly show the described product?"},
        "match_confidence": {"type": "string", "enum": ["confirmed", "potential_risk"]},
        "mismatch_reasoning": {"type": "string", "description": "Why it does/doesn't match. Empty string if it matches fine."},
        "location": {
            "type": "string",
            "description": (
                "Which image/element this refers to, e.g. 'main product photo', 'thumbnail #2', "
                "'lifestyle image in the description'. Required, not optional."
            ),
        },
        "potentially_prohibited": {"type": "boolean"},
        "prohibited_category": {"type": "string", "description": "Which prohibited/restricted category this might raise, or empty string."},
        "prohibited_reasoning": {"type": "string"},
    },
    "required": [
        "plausible_match", "match_confidence", "mismatch_reasoning", "location",
        "potentially_prohibited", "prohibited_category", "prohibited_reasoning",
    ],
}

_SYSTEM_PROMPT = (
    "You are a compliance auditor reviewing one product photo against the product's own page text. "
    "Be conservative: only flag a mismatch if the image clearly does not depict the kind of product described. "
    "For prohibited content, you are a screening step only - flag anything plausibly concerning for a human to "
    "review, but never claim certainty; a human always makes the final determination."
)


def _build_user_text(page: CrawledPage) -> str:
    return (
        f"Product page: {page.url}\n"
        f"Title: {page.title}\n"
        f"Page text (may be truncated): {(page.main_content_text or page.text or '')[:_PAGE_TEXT_LIMIT]}\n\n"
        "Look at the attached image. Does it plausibly show this product? Does it raise any prohibited-content "
        "screening concern? Call the tool with your verdict."
    )


async def check_image_with_vision(client: LLMClient, page: CrawledPage, img: ProductImage) -> list[Finding]:
    result = await client.call_tool_with_image(_SYSTEM_PROMPT, _build_user_text(page), img.url, "submit_image_verdict", _VISION_TOOL_SCHEMA)

    if result is None:
        return [Finding(
            check_id="llm_image_vision_check",
            title="Could not evaluate product image",
            severity=Severity.LOW,
            confidence=Confidence.CANNOT_VERIFY,
            page_url=page.url,
            evidence=f"The LLM API call failed or returned no usable result for image {img.url}.",
            policy_reference="GMC: Product images must show the actual product being sold",
            location=f'img[src="{img.url}"]',
        )]

    findings: list[Finding] = []

    if not result.get("plausible_match"):
        findings.append(Finding(
            check_id="llm_image_product_mismatch",
            title="Product image may not match the product description",
            severity=Severity.MEDIUM,
            confidence=Confidence.CONFIRMED if result.get("match_confidence") == "confirmed" else Confidence.POTENTIAL_RISK,
            page_url=page.url,
            evidence=f"Image {img.url}: {result.get('mismatch_reasoning') or '(no reasoning returned)'}",
            policy_reference="GMC: Product images must show the actual product being sold",
            recommended_fix="Confirm the correct product photo is used on this page.",
            location=result.get("location"),
            from_cache=result.get("_from_cache", False),
        ))

    if result.get("potentially_prohibited"):
        category = result.get("prohibited_category") or "unspecified category"
        findings.append(Finding(
            check_id="llm_image_prohibited_content_flag",
            title=f"Product image flagged for human review ({category})",
            severity=Severity.HIGH,
            # Always potential_risk, never confirmed - this is a screening flag for a
            # human, not a policy determination, regardless of the model's stated confidence.
            confidence=Confidence.POTENTIAL_RISK,
            page_url=page.url,
            evidence=f"Image {img.url}: {result.get('prohibited_reasoning') or '(no reasoning returned)'}",
            policy_reference=get_snippet("prohibited_content").title if get_snippet("prohibited_content") else "GMC prohibited/restricted content policy",
            recommended_fix="Have a human reviewer confirm whether this image violates GMC prohibited-content policy before taking action.",
            location=result.get("location"),
            from_cache=result.get("_from_cache", False),
        ))

    return findings


async def run_llm_image_checks(
    site_map: SiteMap, page_images: dict[str, list[ProductImage]], settings: Settings, cache: LLMCache | None = None,
) -> list[Finding]:
    product_pages_by_url = {p.url: p for p in site_map.pages_of_type(PageType.PRODUCT) if p.reachable}

    # cap: one (first) image per product page, first N product pages that actually have an image
    to_check: list[tuple[CrawledPage, ProductImage]] = []
    for page_url, images in page_images.items():
        page = product_pages_by_url.get(page_url)
        if page is None or not images:
            continue
        to_check.append((page, images[0]))
        if len(to_check) >= _MAX_PRODUCT_PAGES_CHECKED:
            break

    if not to_check:
        return []

    if not settings.llm_configured:
        return [
            Finding(
                check_id="llm_image_vision_check",
                title="Product image not evaluated",
                severity=Severity.LOW,
                confidence=Confidence.CANNOT_VERIFY,
                page_url=page.url,
                evidence=f"API key not configured for LLM_PROVIDER={settings.llm_provider!r} - the LLM vision image check was skipped.",
                policy_reference="GMC: Product images must show the actual product being sold",
                location=f'img[src="{_img.url}"]',
            )
            for page, _img in to_check
        ]

    # Dedup by image URL (hardening round, section 2.3): stores frequently
    # reuse the same image across product variants/pages - group by URL so
    # each unique image is graded once (one API/cache call, not N), with the
    # result applied to every page it appears on.
    pages_by_image_url: dict[str, list[CrawledPage]] = {}
    img_by_url: dict[str, ProductImage] = {}
    for page, img in to_check:
        pages_by_image_url.setdefault(img.url, []).append(page)
        img_by_url[img.url] = img

    client = get_llm_client(settings, cache)
    sem = asyncio.Semaphore(_LLM_CONCURRENCY)

    async def bounded(image_url: str) -> list[Finding]:
        async with sem:
            img = img_by_url[image_url]
            pages = pages_by_image_url[image_url]
            base_findings = await check_image_with_vision(client, pages[0], img)
            all_findings = list(base_findings)
            for extra_page in pages[1:]:
                for f in base_findings:
                    all_findings.append(f.model_copy(update={"page_url": extra_page.url}))
            return all_findings

    results = await asyncio.gather(*(bounded(url) for url in pages_by_image_url), return_exceptions=True)

    findings: list[Finding] = []
    for r in results:
        if isinstance(r, Exception):
            logger.error("LLM image check task raised: %s", r)
            continue
        findings.extend(r)
    return findings
