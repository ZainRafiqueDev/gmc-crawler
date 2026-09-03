"""Step 4: deterministic (non-LLM) checks. Pure logic over an already-built
SiteMap - no network calls except check_broken_images, which needs to probe
asset URLs that were deliberately excluded from the crawl itself.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from difflib import SequenceMatcher

import httpx
from bs4 import BeautifulSoup

from app.fetch import (
    FAILURE_CATEGORY_LABELS,
    FAILURE_CATEGORY_RECOMMENDATIONS,
    FAILURE_CATEGORY_SHORT_LABELS,
    classify_httpx_exception,
)
from app.models import Confidence, CrawledPage, Finding, PageType, Severity, SiteMap
from app.page_classifier import SUPPORTED_LANGUAGES
from app.security.ssrf_guard import safe_async_client

REQUIRED_PAGE_TYPES: dict[PageType, str] = {
    PageType.PRIVACY_POLICY: "Privacy policy",
    PageType.SHIPPING_POLICY: "Shipping policy",
    PageType.RETURNS_POLICY: "Returns/refund policy",
    PageType.TERMS_OF_SERVICE: "Terms of service",
    PageType.CONTACT_ABOUT: "Contact info",
}


def _failure_reason_clause(page: CrawledPage) -> str:
    """Honest, specific "why" for a page this audit couldn't verify (Part 4:
    never just "could not be verified") - reuses the same category labels
    app.fetch attaches at fetch time."""
    label = FAILURE_CATEGORY_LABELS.get(page.failure_category or "unknown", FAILURE_CATEGORY_LABELS["unknown"])
    return f"{label} (fetch error: {page.error})" if page.error else label




def _site_dominant_language(site_map: SiteMap) -> str | None:
    """The most common detected_language among reachable pages, or None if
    no page exposed a <html lang> attribute at all. Used only to decide
    whether a "missing required page" verdict is confident (site content is
    in a language this classifier actually has coverage for) or should be
    downgraded to "could not verify" (app.page_classifier.SUPPORTED_LANGUAGES) -
    a store simply not declaring <html lang> is NOT treated as evidence of
    an unsupported language, since that would silently blunt this check for
    a large share of ordinary (English, undeclared-lang) sites."""
    langs = [p.detected_language for p in site_map.reachable_pages if p.detected_language]
    if not langs:
        return None
    return Counter(langs).most_common(1)[0][0]


def _crawl_incomplete_finding(site_map: SiteMap) -> Finding:
    """A single, honest top-level finding for when literally nothing (or
    only robots.txt-disallowed nothing) could be fetched - replacing what
    would otherwise be several individually-confident "Missing required
    page" findings derived from zero real information. Mirrors the
    previous round's DNS-hiccup fix: never confirm "missing" from an
    incomplete crawl, and now also state *why* it was incomplete (Part 4)."""
    if site_map.robots_disallowed:
        evidence = (
            "This site's robots.txt disallows automated access to the homepage, so no audit could be "
            "performed - out of respect for that, this tool did not attempt to bypass it."
        )
        recommended_fix = "If you own/operate this store and want it audited, update robots.txt to allow this tool's User-Agent, then re-run."
        page_url = site_map.base_url
    else:
        homepage = site_map.pages[0] if site_map.pages else None
        category = homepage.failure_category if homepage else "unknown"
        evidence = (
            f"No page on this site could be successfully fetched during this audit "
            f"({len(site_map.pages)} attempt(s), homepage included) - {_failure_reason_clause(homepage) if homepage else FAILURE_CATEGORY_LABELS['unknown']}. "
            "Privacy/shipping/returns/terms/contact-page presence could not be checked as a result; this is not a confirmed 'missing' verdict."
        )
        recommended_fix = FAILURE_CATEGORY_RECOMMENDATIONS.get(category or "unknown", FAILURE_CATEGORY_RECOMMENDATIONS["unknown"])
        page_url = homepage.url if homepage else site_map.base_url

    return Finding(
        check_id="crawl_incomplete",
        title="Required-page presence could not be checked - this audit could not crawl the site",
        severity=Severity.HIGH,
        confidence=Confidence.CANNOT_VERIFY,
        page_url=page_url,
        evidence=evidence,
        policy_reference="GMC: Store must have required policy pages (unable to confirm - crawl did not complete)",
        recommended_fix=recommended_fix,
        location=None,
    )


def check_https(site_map: SiteMap) -> list[Finding]:
    findings: list[Finding] = []

    home = site_map.pages[0] if site_map.pages else None
    if home and not home.url.startswith("https://"):
        findings.append(Finding(
            check_id="https_enforced",
            title="Homepage does not load over HTTPS",
            severity=Severity.CRITICAL,
            confidence=Confidence.CONFIRMED,
            page_url=home.url,
            evidence=f"Homepage fetched at {home.url} (not https)",
            policy_reference="GMC: Store must use a secure checkout / SSL certificate",
            recommended_fix="Force HTTPS site-wide (redirect http-> https) and update the site URL in WordPress settings.",
            location="document root (page loaded over http:// instead of https://)",
        ))

    for page in site_map.pages:
        if not page.url.startswith("https://"):
            continue
        for link in page.internal_links:
            if link.startswith("http://"):
                findings.append(Finding(
                    check_id="https_mixed_content_link",
                    title="Internal link points to http:// instead of https://",
                    severity=Severity.LOW,
                    confidence=Confidence.CONFIRMED,
                    page_url=page.url,
                    evidence=f"Link to {link} found on {page.url}",
                    policy_reference="GMC: Store must use a secure checkout / SSL certificate",
                    recommended_fix="Update the hardcoded http:// link to https://.",
                    location=f'a[href="{link}"]',
                ))

    return findings


def check_required_pages(site_map: SiteMap) -> list[Finding]:
    # Nothing (or effectively nothing) could be fetched - a confident
    # "missing" verdict for any of the 5 required page types would be
    # asserting a negative from zero real information. One honest finding
    # instead of five overconfident ones (Part 4).
    if site_map.crawl_totally_failed:
        return [_crawl_incomplete_finding(site_map)]

    dominant_language = _site_dominant_language(site_map)
    # A confident "missing" verdict requires the classifier to actually be
    # able to read this site's language - if the dominant detected content
    # language isn't one this round added coverage for (Part 3), a "no
    # matching page found" result may just mean the classifier couldn't
    # recognize a page that's really there, not that it's actually missing.
    language_unsupported = dominant_language is not None and dominant_language not in SUPPORTED_LANGUAGES

    findings: list[Finding] = []
    for page_type, label in REQUIRED_PAGE_TYPES.items():
        matches = site_map.pages_of_type(page_type)
        reachable = [p for p in matches if p.reachable]
        if reachable:
            continue

        cannot_verify_matches = [p for p in matches if p.cannot_verify]
        if cannot_verify_matches:
            page = cannot_verify_matches[0]
            findings.append(Finding(
                check_id="required_page_present",
                title=f"{label} page could not be verified",
                severity=Severity.HIGH,
                confidence=Confidence.CANNOT_VERIFY,
                page_url=page.url,
                evidence=f"A likely {label.lower()} URL was found ({page.url}) but fetch failed after retries: {_failure_reason_clause(page)}",
                policy_reference=f"GMC: Store must have a {label.lower()}",
                recommended_fix=FAILURE_CATEGORY_RECOMMENDATIONS.get(page.failure_category or "unknown", FAILURE_CATEGORY_RECOMMENDATIONS["unknown"]),
                location=None,  # the issue is page-level unreachability, not a specific element
            ))
        elif language_unsupported:
            findings.append(Finding(
                check_id="required_page_present",
                title=f"{label} page could not be confirmed - site content language not recognized",
                severity=Severity.MEDIUM,
                confidence=Confidence.CANNOT_VERIFY,
                page_url=None,
                evidence=(
                    f"No page classified as '{label}' was found in the crawl ({len(site_map.pages)} pages visited), but this site's "
                    f"content appears to be in '{dominant_language}', a language this audit's page classifier does not fully recognize "
                    f"(supported: {', '.join(sorted(SUPPORTED_LANGUAGES))}). A matching page may exist but wasn't automatically identified - "
                    "this is not a confirmed 'missing' verdict."
                ),
                policy_reference=f"GMC: Store must have a {label.lower()}",
                recommended_fix=f"Manually confirm whether a {label.lower()} page exists on this site; automated classification for '{dominant_language}' content is limited.",
                location="site-wide (language-limited classification, not a confirmed absence)",
            ))
        else:
            findings.append(Finding(
                check_id="required_page_present",
                title=f"Missing required page: {label}",
                severity=Severity.CRITICAL,
                confidence=Confidence.CONFIRMED,
                page_url=None,
                evidence=f"No page classified as '{label}' was found anywhere in the crawl ({len(site_map.pages)} pages visited).",
                policy_reference=f"GMC: Store must have a {label.lower()}",
                recommended_fix=f"Add a {label.lower()} page and link it from the site footer.",
                location="site-wide (no matching page found in the crawl)",
            ))
    return findings


def check_external_links(site_map: SiteMap) -> list[Finding]:
    findings: list[Finding] = []
    for page in site_map.pages:
        if not page.reachable:
            continue
        for link in page.external_links:
            findings.append(Finding(
                check_id="external_domain_link",
                title="External-domain link found",
                severity=Severity.LOW,
                confidence=Confidence.CONFIRMED,
                page_url=page.url,
                evidence=f"Link to {link} found on {page.url}",
                policy_reference="GMC: Review for site behavior / unclear business practices if excessive off-site linking",
                recommended_fix="Confirm this external link is intentional (e.g. social/payment provider) and not evidence of scraped/unrelated content.",
                location=f'a[href="{link}"]',
            ))
    return findings


def _block_signature(tag) -> tuple[str, ...]:
    """Cheap structural fingerprint of a DOM block: its link hrefs in order."""
    return tuple(a.get("href", "") for a in tag.find_all("a", href=True))


_MIN_SUBSTANTIAL_LINKS = 4  # skip trivial 1-3 link utility menus (breadcrumbs, skip-links, etc.)
_MIN_DUPLICATE_CLUSTER = 3  # 2 copies is normal responsive theme behavior (desktop + mobile toggle nav)


def check_duplicate_nav_footer(site_map: SiteMap) -> list[Finding]:
    """Flag a <nav>/<footer> block that's rendered 3+ near-identical times on
    one page. Exactly 2 copies is standard responsive-theme behavior (one
    shown, one hidden by CSS for the other breakpoint) and is not flagged -
    3+ usually means a template actually rendered twice by mistake.
    """
    findings: list[Finding] = []
    for page in site_map.pages:
        if not page.reachable or not page.html:
            continue
        soup = BeautifulSoup(page.html, "lxml")

        for tag_name in ("nav", "footer"):
            blocks = soup.find_all(tag_name)
            signatures = [_block_signature(b) for b in blocks]
            substantial = [s for s in signatures if len(s) >= _MIN_SUBSTANTIAL_LINKS]
            if len(substantial) < _MIN_DUPLICATE_CLUSTER:
                continue

            clusters: list[list[tuple[str, ...]]] = []
            for sig in substantial:
                for cluster in clusters:
                    if SequenceMatcher(None, cluster[0], sig).ratio() >= 0.9:
                        cluster.append(sig)
                        break
                else:
                    clusters.append([sig])

            for cluster in clusters:
                if len(cluster) < _MIN_DUPLICATE_CLUSTER:
                    continue
                findings.append(Finding(
                    check_id="duplicate_nav_footer_block",
                    title=f"Same <{tag_name}> block repeated {len(cluster)} times on one page",
                    severity=Severity.MEDIUM,
                    confidence=Confidence.POTENTIAL_RISK,
                    page_url=page.url,
                    evidence=(
                        f"{len(cluster)} near-identical <{tag_name}> blocks (same {len(cluster[0])} links each) "
                        f"found on {page.url}. Two copies is normal for a responsive mobile/desktop menu; "
                        f"three or more usually indicates a duplicated template render."
                    ),
                    policy_reference="GMC: Site must be professionally presented (broken/duplicated templating)",
                    recommended_fix=f"Check the theme/template for a duplicated <{tag_name}> render on this page.",
                    location=f"<{tag_name}> element ({len(cluster)} near-identical copies in the DOM)",
                ))
    return findings


def check_broken_internal_links(site_map: SiteMap) -> list[Finding]:
    # When the crawl totally failed, check_required_pages/
    # check_business_identity_consistency already emit one comprehensive
    # crawl_incomplete-style finding per check explaining why (see
    # _crawl_incomplete_finding). Reporting each individual cannot-verify
    # page here too (often just the homepage, on a single-page crawl) adds
    # no new information - it's the exact same underlying fact restated a
    # third time by a different check, which reads as more independent
    # confirmation than actually exists. Found via live report review:
    # a real totally-failed crawl produced exactly 3 "hidden" lower-
    # priority findings, of which this was the redundant one. A confirmed
    # 404/410 is NOT suppressed here - that's a distinct, independently
    # verified fact (a real broken link) regardless of overall crawl status.
    totally_failed = site_map.crawl_totally_failed
    findings: list[Finding] = []
    for page in site_map.pages:
        if page.reachable:
            continue
        if not page.cannot_verify:
            # confirmed 404/410 - not a reliability failure, a real broken link
            findings.append(Finding(
                check_id="broken_internal_link",
                title="Broken internal link (404)",
                severity=Severity.MEDIUM,
                confidence=Confidence.CONFIRMED,
                page_url=page.url,
                evidence=f"{page.url} returned HTTP {page.status}",
                policy_reference="GMC: Store must be functional and navigable",
                recommended_fix="Fix or remove the link, or restore/redirect the missing page.",
                location=None,  # the whole page 404s - no specific element to point to
            ))
        elif not totally_failed:
            findings.append(Finding(
                check_id="broken_internal_link",
                title=f"Page could not be verified ({FAILURE_CATEGORY_SHORT_LABELS.get(page.failure_category or 'unknown', 'unknown reason')})",
                severity=Severity.LOW,
                confidence=Confidence.CANNOT_VERIFY,
                page_url=page.url,
                evidence=f"{page.url} failed after retries: {_failure_reason_clause(page)}",
                policy_reference="GMC: Store must be functional and navigable",
                recommended_fix=FAILURE_CATEGORY_RECOMMENDATIONS.get(page.failure_category or "unknown", FAILURE_CATEGORY_RECOMMENDATIONS["unknown"]),
                location=None,
            ))
    return findings


async def check_broken_images(site_map: SiteMap, concurrency: int = 8, timeout_seconds: float = 8.0) -> list[Finding]:
    unique_images: dict[str, str] = {}  # image_url -> first page that referenced it
    for page in site_map.pages:
        for img in page.image_srcs:
            unique_images.setdefault(img, page.url)

    if not unique_images:
        return []

    findings: list[Finding] = []
    sem = asyncio.Semaphore(concurrency)
    timeout = httpx.Timeout(timeout_seconds)

    async def probe(client: httpx.AsyncClient, img_url: str, source_page: str) -> None:
        async with sem:
            try:
                resp = await client.head(img_url, timeout=timeout, follow_redirects=True)
                if resp.status_code == 405:  # HEAD not allowed - fall back to GET
                    resp = await client.get(img_url, timeout=timeout, follow_redirects=True)
                if resp.status_code >= 400:
                    findings.append(Finding(
                        check_id="broken_image",
                        title="Broken image",
                        severity=Severity.LOW,
                        confidence=Confidence.CONFIRMED,
                        page_url=source_page,
                        evidence=f"Image {img_url} (referenced on {source_page}) returned HTTP {resp.status_code}",
                        policy_reference="GMC: Store must be professionally presented",
                        recommended_fix="Fix or remove the broken image reference.",
                        location=f'img[src="{img_url}"]',
                    ))
            except httpx.HTTPError as exc:
                category = classify_httpx_exception(exc)
                reason = FAILURE_CATEGORY_SHORT_LABELS.get(category, "unknown reason")
                findings.append(Finding(
                    check_id="broken_image",
                    title=f"Image could not be verified ({reason})",
                    severity=Severity.LOW,
                    confidence=Confidence.CANNOT_VERIFY,
                    page_url=source_page,
                    evidence=f"Image {img_url} (referenced on {source_page}) failed to load - {reason}: {exc}",
                    policy_reference="GMC: Store must be professionally presented",
                    recommended_fix=FAILURE_CATEGORY_RECOMMENDATIONS.get(category, FAILURE_CATEGORY_RECOMMENDATIONS["unknown"]),
                    location=f'img[src="{img_url}"]',
                ))

    async with safe_async_client() as client:
        await asyncio.gather(*(probe(client, url, src) for url, src in unique_images.items()))

    return findings


async def run_all_deterministic_checks(site_map: SiteMap) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(check_https(site_map))
    findings.extend(check_required_pages(site_map))
    findings.extend(check_external_links(site_map))
    findings.extend(check_duplicate_nav_footer(site_map))
    findings.extend(check_broken_internal_links(site_map))
    findings.extend(await check_broken_images(site_map))
    return findings
