"""Deterministic check for duplicate/near-duplicate product listings across
different URLs. GMC's own editorial-quality guidance explicitly names this:
"The same product details being used for multiple products without
differentiation" is listed as an example of what's not allowed (retrieved
live from the real Phase C RAG index for editorial_quality - see
validation/impact_tier_grounding.md) - grounding this as a
quality_improvement-tier finding, not a guess.

Exact-title duplicates are grouped in one O(n) pass (a normalized-title
dict). Near-duplicates use difflib.SequenceMatcher, bounded to pairs that
share the same first significant word - full O(n^2) comparison across an
entire large catalog isn't worth it for a low-severity quality signal.
"""
from __future__ import annotations

import re
from collections import defaultdict
from difflib import SequenceMatcher

from app.models import Confidence, CrawledPage, Finding, PageType, Severity, SiteMap

_WHITESPACE_RE = re.compile(r"\s+")
_NEAR_DUPLICATE_THRESHOLD = 0.92

_POLICY_REFERENCE = (
    'GMC policy: Editorial and professional content quality [editorial_quality] - '
    'https://support.google.com/merchants/answer/12079604 ("Examples of what\'s not allowed")'
)
_POLICY_REQUIREMENT_TEXT = (
    "Examples of what's not allowed: Product descriptions and specifications that are unrelated to the "
    "product image or listing. The same product details being used for multiple products without "
    "differentiation. Ensure the product details on the website are accurate and unique for each product."
)


def _normalize_title(title: str) -> str:
    return _WHITESPACE_RE.sub(" ", title.strip().lower())


def _product_title(page: CrawledPage) -> str | None:
    # Prefer the first heading (usually the real product name in the page
    # body) over <title>, which typically has a " - Site Name" suffix that
    # would otherwise make identical products compare as different titles.
    if page.headings and page.headings[0].strip():
        return page.headings[0].strip()
    if page.title and page.title.strip():
        return page.title.strip()
    return None


def _duplicate_finding(title: str, urls: list[str], *, exact: bool, other_title: str | None = None) -> Finding:
    kind = "identical" if exact else "near-identical"
    compare_note = f' (compared against "{other_title}")' if other_title else ""
    return Finding(
        check_id="duplicate_product_listing",
        title=f"Duplicate product listing: {kind} title across {len(urls)} URLs",
        severity=Severity.LOW,
        confidence=Confidence.CONFIRMED if exact else Confidence.POTENTIAL_RISK,
        page_url=urls[0],
        evidence=f'Product title "{title}"{compare_note} appears on {len(urls)} different URLs: {", ".join(urls)}',
        policy_reference=_POLICY_REFERENCE,
        policy_requirement_text=_POLICY_REQUIREMENT_TEXT,
        recommended_fix="Consolidate these into a single listing, or differentiate the product title/description for each so they aren't duplicates of each other.",
        location="product title",
    )


def check_duplicate_products(site_map: SiteMap) -> list[Finding]:
    products = [p for p in site_map.pages if p.page_type == PageType.PRODUCT and p.reachable]
    titled = [(p, _product_title(p)) for p in products]
    titled = [(p, t) for p, t in titled if t]

    findings: list[Finding] = []
    flagged_pairs: set[frozenset[str]] = set()

    by_normalized: dict[str, list[tuple[CrawledPage, str]]] = defaultdict(list)
    for page, title in titled:
        by_normalized[_normalize_title(title)].append((page, title))

    for group in by_normalized.values():
        distinct_urls = sorted({p.url for p, _ in group})
        if len(distinct_urls) < 2:
            continue
        flagged_pairs.add(frozenset(distinct_urls))
        findings.append(_duplicate_finding(group[0][1], distinct_urls, exact=True))

    buckets: dict[str, list[tuple[CrawledPage, str, str]]] = defaultdict(list)
    for page, title in titled:
        normalized = _normalize_title(title)
        first_word = normalized.split(" ", 1)[0] if normalized else ""
        buckets[first_word].append((page, title, normalized))

    for bucket in buckets.values():
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                page_a, title_a, norm_a = bucket[i]
                page_b, title_b, norm_b = bucket[j]
                if norm_a == norm_b or page_a.url == page_b.url:
                    continue  # exact duplicates already handled above
                pair_key = frozenset([page_a.url, page_b.url])
                if pair_key in flagged_pairs:
                    continue
                if SequenceMatcher(None, norm_a, norm_b).ratio() < _NEAR_DUPLICATE_THRESHOLD:
                    continue
                flagged_pairs.add(pair_key)
                findings.append(_duplicate_finding(title_a, [page_a.url, page_b.url], exact=False, other_title=title_b))

    return findings
