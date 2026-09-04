"""Step 6: compile all findings into the Markdown report shape from the
project brief. A page's pass/risk/cannot-verify status in the page-by-page
section is derived from the finding list, not tracked separately - a page
with no findings against it passed; a page with a critical/high finding (or
any cannot-verify finding) is flagged accordingly.

Report structure (Store-Overview-first + suspension-risk-prioritized
restructuring, later given a narrative pass): the report leads with a
prose summary and an explainable internal risk score, then what could
actually get a store's GMC account suspended (with the real RAG-retrieved
policy text behind each one), then a Policy-by-Policy matrix, then a
deprioritized (but never dropped) section for everything else, then a
page-by-page section split into Store Overview (homepage/policies/contact)
vs a grouped Catalog overview (products/collections - grouped by logical
page after query-param/pagination canonicalization, not one row per literal
URL), then a closing narrative assessment.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

from app.fetch import FAILURE_CATEGORY_LABELS, FAILURE_CATEGORY_RECOMMENDATIONS, FAILURE_CATEGORY_SHORT_LABELS
from app.impact_tier import policy_area_for_finding
from app.models import (
    AdsEligibilityImpact, Confidence, CrawledPage, Finding, ImpactTier, LLMCoverageStats, PageType, Platform,
    PlatformDetectionResult, Severity, SiteMap, VerificationMethod,
)
from app.security.sanitize import sanitize_for_report
from app.url_canonicalize import canonical_page_key

_UNSAFE_FILENAME_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_host_for_filename(url: str) -> str:
    """Host portion of a URL, sanitized for use in a filename. `netloc`
    can contain a `:port` - on Windows, a bare `:` in a filename is parsed
    as an Alternate Data Stream separator (`name:stream`), which silently
    creates an empty file instead of the intended report, so every
    non-alphanumeric character gets collapsed to `-`.
    """
    host = urlparse(url if "://" in url else f"https://{url}").netloc or "site"
    return _UNSAFE_FILENAME_CHARS_RE.sub("-", host).strip("-")

_SEVERITY_ORDER = [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW]
_SEVERITY_LABEL = {
    Severity.CRITICAL: "Critical issues",
    Severity.HIGH: "High-priority issues",
    Severity.MEDIUM: "Medium issues",
    Severity.LOW: "Low issues",
}

# Catalog = the product/collection surface; everything else (homepage,
# policies, contact, cart/checkout, FAQ, blog) is Store Overview - a
# deliberately simple binary split matching how the crawler itself now
# prioritizes (app/page_classifier.py's overview/catalog priority tiers).
CATALOG_PAGE_TYPES = frozenset({PageType.PRODUCT, PageType.COLLECTION})

_TIER_ORDER = [ImpactTier.SUSPENSION_RISK, ImpactTier.LISTING_DISAPPROVAL, ImpactTier.QUALITY_IMPROVEMENT]
_TIER_LABEL = {
    ImpactTier.SUSPENSION_RISK: "Suspension Risk (lower severity or unconfirmed)",
    ImpactTier.LISTING_DISAPPROVAL: "Listing Disapproval Risk",
    ImpactTier.QUALITY_IMPROVEMENT: "Quality Improvements",
}


def is_catalog_page(page: CrawledPage) -> bool:
    return page.page_type in CATALOG_PAGE_TYPES


def is_suspension_risk_finding(f: Finding) -> bool:
    """The report's primary/default view: a Critical-severity finding, or
    any severity of a finding whose check is grounded (app/impact_tier.py)
    in GMC policy text tied to account-level suspension - excluding
    CANNOT_VERIFY findings, which aren't a confirmed problem regardless of
    tier. This is also what major_only=True filters to.
    """
    if f.confidence == Confidence.CANNOT_VERIFY:
        return False
    return f.severity == Severity.CRITICAL or f.impact_tier == ImpactTier.SUSPENSION_RISK


def _page_status(page: CrawledPage, page_findings: list[Finding]) -> str:
    if page.cannot_verify:
        return "CANNOT VERIFY"
    # A confirmed-unreachable page (e.g. a real 404, or blocked_ssrf) with no
    # findings against it must never read as "PASS" - that word means "we
    # checked and it's fine", not "we couldn't load it". Checked ahead of
    # cannot_verify's "checked but unreachable" case (already handled above).
    if not page.reachable:
        return "UNREACHABLE"
    if any(f.severity in (Severity.CRITICAL, Severity.HIGH) for f in page_findings):
        return "RISK"
    if any(f.confidence == Confidence.CANNOT_VERIFY for f in page_findings):
        return "CANNOT VERIFY"
    if page_findings:
        return "RISK"
    return "PASS"


# Same wording app.llm.checks._NOTE_EVIDENCE_NOT_VERIFIED uses internally -
# duplicated as a plain string rather than imported, to keep this reporting
# module decoupled from the LLM-checks module (report.py already renders
# findings from every check family, deterministic included, and shouldn't
# need an import from one specific LLM check module just for this string).
_NOTE_EVIDENCE_NOT_VERIFIED = "Evidence quote could not be independently verified as exact page text."

_VERIFICATION_LABEL = {
    VerificationMethod.API_VERIFIED: "API-verified",
    VerificationMethod.PAGE_ONLY: "best-effort (page-only)",
}


def _format_timestamp(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _format_finding(f: Finding) -> str:
    # Every field below can contain content lifted from the audited (possibly
    # malicious/compromised) site - sanitize before it goes into the report.
    title = sanitize_for_report(f.title)
    location = sanitize_for_report(f.location) or "page-level (no specific element)"
    evidence = sanitize_for_report(f.evidence)
    policy_reference = sanitize_for_report(f.policy_reference)
    recommended_fix = sanitize_for_report(f.recommended_fix)
    # Same real RAG-retrieved policy text and GMC Help Center source URL
    # _format_finding_rich shows for Suspension Risk findings - these are
    # NOT suspension-risk-exclusive data (Finding.policy_requirement_text
    # is set at grading time by any LLM-graded check, regardless of which
    # impact tier it's later classified into); the previous omission here
    # meant a real llm_editorial_quality (quality_improvement-tier) finding
    # with genuine RAG-grounded text was silently dropping that grounding
    # the moment it landed in "Other Findings" instead of "Suspension Risk"
    # - found live against a real store (meo.fr).
    requirement = sanitize_for_report(f.policy_requirement_text)
    source_url = _extract_source_url(f.policy_reference)

    lines = [
        f"- **{title}**",
        f"  - Severity: {f.severity.value} | Confidence: {f.confidence.value} | Verification: {_VERIFICATION_LABEL[f.verification_method]} | Impact: {f.impact_tier.value} | Ads eligibility: {f.ads_eligibility_impact.value}",
    ]
    if f.page_url:
        lines.append(f"  - Page: {f.page_url}")
    lines.append(f"  - Location: {location}")
    lines.append(f"  - Detected: {_format_timestamp(f.detected_at)}")
    lines.append(f"  - Evidence: {evidence}")
    if not f.evidence_verified:
        lines.append(f"  - {_NOTE_EVIDENCE_NOT_VERIFIED}")
    if policy_reference:
        lines.append(f"  - Policy: {policy_reference}")
    if requirement:
        lines.append(f'  - Specific Policy Requirement: "{requirement}"')
    if recommended_fix:
        lines.append(f"  - Recommended fix: {recommended_fix}")
    if source_url:
        lines.append(f"  - Official Source: {_source_url_with_freshness(source_url, f.policy_last_verified)}")
    return "\n".join(lines)


def _format_finding_compact(f: Finding) -> str:
    """One-line form used in the page-by-page section, matching the shape:
    `[HIGH] Title — location — detected 2026-08-26 14:32 UTC`
    """
    title = sanitize_for_report(f.title)
    location = sanitize_for_report(f.location) or "page-level"
    return f"[{f.severity.value.upper()}] {title} — {location} — detected {_format_timestamp(f.detected_at)}"


_URL_IN_TEXT_RE = re.compile(r"https?://\S+")


def _extract_source_url(policy_reference: str | None) -> str | None:
    """Pulls the real GMC Help Center URL already embedded in a citation
    string like 'GMC policy: ... - https://support.google.com/... ("Section")'
    (app/llm/checks.py::_policy_reference) - not a new lookup, just surfacing
    a URL that's already there as its own labeled field."""
    if not policy_reference:
        return None
    match = _URL_IN_TEXT_RE.search(policy_reference)
    if not match:
        return None
    return match.group(0).rstrip(').,;:"')


def _llm_coverage_sentence(llm_coverage: LLMCoverageStats | None) -> str | None:
    """Follow-up round fix: check_editorial_quality/check_prohibited_content
    sample a fixed number of product pages regardless of catalog size - a
    "Pass" on Prohibited Content for a 500-product store could otherwise
    read as if 500 products were screened. None when there's nothing
    meaningful to report (no llm_coverage supplied at all, or the store
    simply has no product pages - not a coverage gap, not applicable)."""
    if llm_coverage is None or llm_coverage.total_reachable_product_pages == 0:
        return None
    if not llm_coverage.llm_configured:
        return (
            f"Prohibited-content / editorial-quality screening: not run for any of "
            f"{llm_coverage.total_reachable_product_pages} product page(s) - no LLM provider configured."
        )
    pct = f"{llm_coverage.coverage_fraction:.0%}" if llm_coverage.coverage_fraction is not None else "n/a"
    return (
        f"Prohibited-content / editorial-quality screening: {llm_coverage.product_pages_checked} of "
        f"{llm_coverage.total_reachable_product_pages} product page(s) checked ({pct})"
    )


def _source_url_with_freshness(source_url: str, last_verified) -> str:
    """Appends "(last verified: YYYY-MM-DD)" when we know it - Part 4 of
    the follow-up round: the RAG index already tracks when each policy
    chunk was last confirmed against the live Google page
    (app.llm.policy_rag.PolicyContext.verified_at, itself
    PolicyChunk.created_at - set only when app.policy_watcher actually
    re-scraped it), reused here rather than a new lookup. Omitted (not a
    placeholder date) when unknown - a deterministic check's static
    citation, or a stub-snippet fallback, has no real verification date to
    show."""
    if last_verified is None:
        return source_url
    return f"{source_url} (last verified: {last_verified.strftime('%Y-%m-%d')})"


_WHY_IT_MATTERS = {
    ImpactTier.SUSPENSION_RISK: (
        "Google's own enforcement guidance ties this class of issue to whole-account suspension, not just a "
        "single listing being rejected."
    ),
    ImpactTier.LISTING_DISAPPROVAL: (
        "This is most likely to get the specific affected product listing(s) disapproved, without necessarily "
        "affecting the rest of the account."
    ),
    ImpactTier.QUALITY_IMPROVEMENT: (
        "This does not directly risk enforcement on its own, but addressing it improves overall listing quality "
        "and customer trust."
    ),
}

# Part 1 of the follow-up round (ads-eligibility tagging) - see
# app/ads_eligibility.py for the real-policy-text grounding behind these.
_ADS_ELIGIBILITY_LABEL = {
    AdsEligibilityImpact.ADS_AND_LISTINGS: "Affects both paid Shopping ads and free-listing eligibility",
    AdsEligibilityImpact.LISTINGS_ONLY: "Affects free-listing eligibility specifically",
    AdsEligibilityImpact.UNCLEAR: "Not attributed to a specific GMC policy area - ads/listings impact unclear",
}


def _format_finding_rich(f: Finding) -> str:
    """The richer per-finding structure for prominent (Suspension Risk)
    findings: adds Specific Policy Requirement (the real retrieved RAG
    text - app/models.py's Finding.policy_requirement_text), Why It
    Matters, and Official Source (the real source URL already embedded in
    policy_reference) on top of the compact format's fields.
    """
    title = sanitize_for_report(f.title)
    location = sanitize_for_report(f.location) or "page-level (no specific element)"
    evidence = sanitize_for_report(f.evidence)
    policy_reference = sanitize_for_report(f.policy_reference)
    requirement = sanitize_for_report(f.policy_requirement_text)
    recommended_fix = sanitize_for_report(f.recommended_fix)
    source_url = _extract_source_url(f.policy_reference)

    lines = [
        f"### {title}",
        f"- **Severity:** {f.severity.value} | **Confidence:** {f.confidence.value} | **Verification:** {_VERIFICATION_LABEL[f.verification_method]}",
    ]
    if f.page_url:
        lines.append(f"- **Page URL:** {f.page_url}")
    lines.append(f"- **Location:** {location}")
    lines.append(f"- **Evidence:** {evidence}")
    if not f.evidence_verified:
        lines.append(f"- **Note:** {_NOTE_EVIDENCE_NOT_VERIFIED}")
    if f.screenshot_path:
        # Relative to Settings.report_output_dir (where this report itself
        # is written) - a plain Markdown image reference, resolved the same
        # way by docx/PDF export (app.report_docx/app.report_pdf).
        lines.append(f"- **Screenshot:** ![Annotated screenshot for {title}]({f.screenshot_path})")
    if policy_reference:
        lines.append(f"- **Relevant Policy:** {policy_reference}")
    if requirement:
        lines.append(f'- **Specific Policy Requirement:** "{requirement}"')
    lines.append(f"- **Why It Matters:** {_WHY_IT_MATTERS[f.impact_tier]}")
    lines.append(f"- **Ads Eligibility Impact:** {_ADS_ELIGIBILITY_LABEL[f.ads_eligibility_impact]}")
    if recommended_fix:
        lines.append(f"- **Recommended Fix:** {recommended_fix}")
    if source_url:
        lines.append(f"- **Official Source:** {_source_url_with_freshness(source_url, f.policy_last_verified)}")
    return "\n".join(lines)


def _per_page_findings_lines(page: CrawledPage, page_findings: list[Finding], major_only: bool) -> tuple[str, list[str]]:
    """Shared by the Store Overview per-page block and the Catalog group
    block: renders whichever findings are displayed at this priority level,
    plus a count of what's on this page/group but not itemized here (Part
    3's decluttering goal - never fully hide that lower-tier findings
    exist, just don't print every one of them in the primary view).
    """
    shown = [f for f in page_findings if not major_only or is_suspension_risk_finding(f)]
    other_count = len(page_findings) - len(shown)

    lines: list[str] = []
    status = _page_status(page, shown)
    if shown:
        for f in shown:
            lines.append(f"  - {_format_finding_compact(f)}")
            note = f" | {_NOTE_EVIDENCE_NOT_VERIFIED}" if not f.evidence_verified else ""
            lines.append(f"      Confidence: {f.confidence.value} | Verification: {_VERIFICATION_LABEL[f.verification_method]} | Evidence: {sanitize_for_report(f.evidence)}{note}")
    elif major_only:
        lines.append("- No suspension-risk issues found.")
    else:
        lines.append("- No issues found.")
    if major_only and other_count:
        lines.append(f"- {other_count} other (lower-priority) finding(s) here - not itemized in this view, see the full report.")
    return status, lines


def _page_by_page_block(pages: list[CrawledPage], all_findings_by_page: dict[str, list[Finding]], major_only: bool) -> list[str]:
    lines: list[str] = []
    for page in sorted(pages, key=lambda p: (p.depth, p.url)):
        page_findings = all_findings_by_page.get(page.url, [])
        status, findings_lines = _per_page_findings_lines(page, page_findings, major_only)
        lines.append(f"### [{status}] {page.url}")
        lines.append(f"- Type: {page.page_type.value} | Depth: {page.depth} | Status: {page.status}")
        if page.title:
            lines.append(f"- Title: {sanitize_for_report(page.title)}")
        if not page.reachable:
            lines.append(f"- Fetch error: {sanitize_for_report(page.error)}")
            if page.cannot_verify and page.failure_category:
                lines.append(f"- Reason: {FAILURE_CATEGORY_LABELS.get(page.failure_category, FAILURE_CATEGORY_LABELS['unknown'])}")
        lines.extend(findings_lines)
        lines.append("")
    return lines


def _catalog_section_lines(catalog_pages: list[CrawledPage], all_findings_by_page: dict[str, list[Finding]], major_only: bool) -> list[str]:
    """Groups the (potentially hundreds of) crawled catalog pages by
    canonical logical page (app/url_canonicalize.py - collapses pagination
    variants of the same category for display, on top of the facet-query
    variants already never separately crawled at all - see
    app/site_mapper.py's _normalize). Only groups with a displayed finding
    are itemized individually; everything else is rolled into a single
    summary line so a reader isn't shown hundreds of "no issues" entries,
    without erasing the fact that lower-tier findings exist elsewhere.
    """
    groups: dict[str, list[CrawledPage]] = defaultdict(list)
    for p in catalog_pages:
        groups[canonical_page_key(p.url)].append(p)

    flagged_lines: list[str] = []
    flagged_group_count = 0
    clean_group_count = 0
    other_only_group_count = 0
    other_only_finding_count = 0

    for key in sorted(groups):
        group_pages = groups[key]
        representative = min(group_pages, key=lambda p: len(p.url))
        group_findings = [f for p in group_pages for f in all_findings_by_page.get(p.url, [])]
        status, findings_lines = _per_page_findings_lines(representative, group_findings, major_only)

        shown_any = any(not major_only or is_suspension_risk_finding(f) for f in group_findings)
        if shown_any:
            flagged_group_count += 1
            flagged_lines.append(f"### [{status}] {key}")
            variant_note = f" ({len(group_pages)} page variant(s) checked, incl. pagination)" if len(group_pages) > 1 else ""
            flagged_lines.append(f"- Type: {representative.page_type.value}{variant_note}")
            flagged_lines.extend(findings_lines)
            flagged_lines.append("")
        elif group_findings:
            other_only_group_count += 1
            other_only_finding_count += len(group_findings)
        else:
            clean_group_count += 1

    total_pages_crawled = len(catalog_pages)
    lines = [
        "### Catalog Overview",
        "",
        f"- {len(groups)} distinct catalog page(s)/section(s) crawled ({total_pages_crawled} page fetch(es) total, including pagination variants; facet/filter query-parameter duplicates were never separately crawled).",
    ]
    if major_only:
        lines.append(f"- {flagged_group_count} with a suspension-risk issue (see below), {other_only_group_count} with only lower-priority findings, {clean_group_count} with no findings at all.")
        if other_only_finding_count:
            lines.append(f"- {other_only_finding_count} lower-priority finding(s) across those {other_only_group_count} section(s) are not itemized in this view - see the full report.")
    else:
        lines.append(f"- {flagged_group_count} with at least one finding, {clean_group_count} with none.")
    lines.append("")
    lines.extend(flagged_lines)
    return lines


# --- Policy-by-Policy Review matrix (Part 4.2) ---------------------------

_POLICY_AREA_LABELS: dict[str, str] = {
    "shipping_policy": "Shipping Policy",
    "returns_refunds": "Returns & Refunds",
    "business_identity": "Business Identity",
    "misrepresentation": "Misrepresentation",
    "prohibited_content": "Prohibited Content",
    "privacy_policy": "Privacy Policy",
    "terms_of_service": "Terms of Service",
    "editorial_quality": "Editorial Quality",
}
_POLICY_AREA_ORDER = list(_POLICY_AREA_LABELS.keys())


_LLM_SAMPLED_POLICY_AREAS = frozenset({"prohibited_content", "editorial_quality"})


def _build_policy_matrix(findings: list[Finding], crawl_totally_failed: bool = False, llm_coverage: LLMCoverageStats | None = None) -> list[str]:
    lines = [
        "## Policy-by-Policy Review", "",
        "| Policy Area | Status | Findings | Summary |",
        "| --- | --- | --- | --- |",
    ]
    if crawl_totally_failed:
        # Every area would otherwise show "Pass, 0 findings" here - the
        # crawl_incomplete/business_identity_crawl_incomplete findings don't
        # map to any of these 8 areas via policy_area_for_finding, so with
        # nothing else in `findings` this table would misleadingly look like
        # a clean sweep instead of "nothing was actually checked." Found via
        # live testing (a real store whose robots.txt disallowed the crawl) -
        # see the "This Audit Could Not Run" banner this table sits below.
        for area in _POLICY_AREA_ORDER:
            lines.append(f"| {_POLICY_AREA_LABELS[area]} | Cannot Verify | 0 | Audit did not complete - see \"This Audit Could Not Run\" above. |")
        return lines
    for area in _POLICY_AREA_ORDER:
        area_findings = [f for f in findings if policy_area_for_finding(f) == area]
        confirmed = [f for f in area_findings if f.confidence == Confidence.CONFIRMED]
        potential_risk = [f for f in area_findings if f.confidence == Confidence.POTENTIAL_RISK]
        cannot_verify_only = area_findings and not confirmed and not potential_risk

        # Confidence-aware status (follow-up round, Part 2): a policy area
        # with one CONFIRMED issue reads very differently from one with
        # nothing but POTENTIAL_RISK findings (e.g. 80 external links) -
        # both used to show the same blanket "At Risk". Reuses the existing
        # Confidence field directly, no new data needed. Priority order
        # matches the severity of the claim being made: Fail (a real,
        # confirmed problem) outranks At Risk (unconfirmed but flagged)
        # outranks Cannot Verify (no confirmed information either way)
        # outranks Pass (nothing found).
        if confirmed:
            status = "Fail"
        elif potential_risk:
            status = "At Risk"
        elif cannot_verify_only:
            status = "Cannot Verify"
        else:
            status = "Pass"

        # A "Pass" earned from a fixed-size product-page sample reads
        # identically to one where the whole catalog was checked, unless
        # this is stated - a 2%-sampled Pass on a 500-product store is not
        # the same claim as a 100%-sampled one. Only annotates Pass (a
        # Fail/At Risk/Cannot Verify already tells the reader something
        # concrete, regardless of sample size) and only the two areas these
        # two specific LLM checks actually cover.
        if status == "Pass" and area in _LLM_SAMPLED_POLICY_AREAS and llm_coverage is not None and llm_coverage.is_partial:
            status = f"Pass (partial coverage: {llm_coverage.product_pages_checked}/{llm_coverage.total_reachable_product_pages})"

        if confirmed:
            worst = min(confirmed, key=lambda f: _SEVERITY_ORDER.index(f.severity))
            summary = sanitize_for_report(worst.title)
        elif potential_risk:
            worst = min(potential_risk, key=lambda f: _SEVERITY_ORDER.index(f.severity))
            summary = sanitize_for_report(worst.title)
        elif cannot_verify_only:
            summary = "Could not be verified during this crawl."
        else:
            summary = "No issues found for this policy area."
        summary = summary.replace("|", "/")

        lines.append(f"| {_POLICY_AREA_LABELS[area]} | {status} | {len(area_findings)} | {summary} |")
    return lines


# --- Internal risk score (Part 4.5) --------------------------------------

_SUSPENSION_DEDUCTION = {Severity.CRITICAL: 25, Severity.HIGH: 15, Severity.MEDIUM: 8, Severity.LOW: 4}
_DISAPPROVAL_DEDUCTION = {Severity.CRITICAL: 10, Severity.HIGH: 5, Severity.MEDIUM: 2, Severity.LOW: 1}
_QUALITY_DEDUCTION_PER_FINDING = 0.5


@dataclass
class RiskScore:
    score: int | None
    rating: str
    breakdown: list[str] = field(default_factory=list)
    # Single source of truth for "this audit didn't produce a real
    # assessment" - every renderer of the score/rating (At a Glance,
    # Executive Summary, Final Assessment) checks *this*, not its own
    # separate crawl_totally_failed lookup. That per-site duplication is
    # exactly how the At a Glance section fell out of sync with the rest of
    # the report (Final Assessment/Policy Matrix got the fix, this one
    # didn't) - fixing it at the one place the value is computed instead.
    not_applicable: bool = False


def compute_risk_score(findings: list[Finding], crawl_totally_failed: bool = False) -> RiskScore:
    """An internal, explainable policy-risk score - NOT a Google score and
    NOT a prediction of approval or suspension (stated explicitly wherever
    this is rendered). Every point deducted is itemized in `breakdown` so
    the number is reproducible from the report's own findings, not opaque.

    crawl_totally_failed=True short-circuits to a "not applicable" result -
    a numeric score computed from zero real information is not meaningful,
    regardless of what `findings` happens to contain (typically just the
    honest crawl_incomplete/business_identity_crawl_incomplete findings,
    which carry no severity/tier information worth scoring).
    """
    if crawl_totally_failed:
        return RiskScore(
            score=None, rating="N/A", not_applicable=True,
            breakdown=["This audit did not complete, so no risk score is meaningful - see \"This Audit Could Not Run\" above."],
        )

    confirmed = [f for f in findings if f.confidence != Confidence.CANNOT_VERIFY]
    suspension = [f for f in confirmed if is_suspension_risk_finding(f)]
    disapproval = [f for f in confirmed if f not in suspension and f.impact_tier == ImpactTier.LISTING_DISAPPROVAL]
    quality = [f for f in confirmed if f not in suspension and f.impact_tier == ImpactTier.QUALITY_IMPROVEMENT]

    score = 100.0
    breakdown: list[str] = []

    for sev in _SEVERITY_ORDER:
        count = sum(1 for f in suspension if f.severity == sev)
        if not count:
            continue
        per_finding = _SUSPENSION_DEDUCTION[sev]
        deduction = count * per_finding
        score -= deduction
        breakdown.append(f"-{deduction:g}: {count} suspension-risk finding(s) at {sev.value} severity ({per_finding} pt each)")

    for sev in _SEVERITY_ORDER:
        count = sum(1 for f in disapproval if f.severity == sev)
        if not count:
            continue
        per_finding = _DISAPPROVAL_DEDUCTION[sev]
        deduction = count * per_finding
        score -= deduction
        breakdown.append(f"-{deduction:g}: {count} listing-disapproval finding(s) at {sev.value} severity ({per_finding} pt each)")

    if quality:
        deduction = len(quality) * _QUALITY_DEDUCTION_PER_FINDING
        score -= deduction
        breakdown.append(f"-{deduction:g}: {len(quality)} quality-improvement finding(s) ({_QUALITY_DEDUCTION_PER_FINDING} pt each)")

    if not breakdown:
        breakdown.append("No confirmed findings to deduct for - starting score of 100 stands.")

    # Explicit accounting for the gap between len(findings) and how many of
    # them the deductions above actually cover - a reader summing the
    # itemized counts against the report's own total-findings count
    # otherwise has to already know CANNOT_VERIFY findings are excluded by
    # design (an unconfirmed issue shouldn't move a confidence-based score)
    # rather than being told so. Found live: a real report's breakdown
    # summed to 150 findings out of 170 total with no stated reason for
    # the other 20.
    excluded_count = len(findings) - len(confirmed)
    if excluded_count:
        breakdown.append(
            f"{excluded_count} finding(s) excluded from scoring: cannot-verify (an unconfirmed issue doesn't move this score either way)."
        )

    raw_score = round(score)
    final_score = max(0, min(100, raw_score))
    if raw_score != final_score:
        # The itemized deductions above are the real, reproducible math -
        # they can (and on a store with many suspension-risk findings,
        # will) sum past 0. Stating the clamp explicitly here is what makes
        # the report's own "reproducible from its own findings" claim
        # actually true for a reader working the numbers by hand - without
        # this line, a report showing "0/100" with deductions that sum well
        # past -100 looks like the math doesn't add up, when it's actually
        # just an unstated floor. Found live (a real report: deductions
        # summed to -195, displayed score was 0/100, with no explanation).
        verb = "floored" if final_score == 0 else "capped"
        breakdown.append(f"Raw score before clamping: {raw_score:g} - {verb} to {final_score} (score is always shown in the 0-100 range).")
    has_critical_suspension = any(f.severity == Severity.CRITICAL for f in suspension)
    if has_critical_suspension or final_score < 50:
        rating = "HIGH"
    elif final_score < 80:
        rating = "MEDIUM"
    else:
        rating = "LOW"

    return RiskScore(score=final_score, rating=rating, breakdown=breakdown)


_RISK_SCORE_DISCLAIMER = (
    "This is an internal policy-risk score computed from this audit's own findings - it is not a Google "
    "score, not issued by Google, and not a prediction of actual approval or suspension."
)


# --- API-verification gap recommendation (Part 5) ------------------------

def _api_verification_recommendation(platform: PlatformDetectionResult, findings: list[Finding]) -> str | None:
    api_verified_count = sum(1 for f in findings if f.verification_method == VerificationMethod.API_VERIFIED)
    if api_verified_count > 0 or platform.platform != Platform.WOOCOMMERCE:
        return None
    evidence_note = f" ({sanitize_for_report(platform.evidence[-1])})" if platform.evidence else ""
    return (
        "**Recommendation: connect the WooCommerce REST API for a stronger audit.** "
        f"This store's REST API route was detected but this audit could not authenticate against it{evidence_note}, "
        "so every product-data finding above is best-effort page scraping rather than API-verified. Supply "
        "`WC_CONSUMER_KEY`/`WC_CONSUMER_SECRET` (see `.env.example`, or `--wc-key`/`--wc-secret` on the CLI) to "
        "enable API-verified price/stock/availability checks - a materially stronger audit for GMC compliance."
    )


# --- Honest crawl-failure reporting (Part 4) ------------------------------

def _crawl_failure_banner(site_map: SiteMap) -> str | None:
    """A prominent, honest banner for when this tool could not meaningfully
    crawl the site at all - shown regardless of major_only, since a total
    crawl failure is not a lower-priority finding to be filtered out. This
    overrides what would otherwise be misleading "found no issues" happy-
    path language in the executive summary (below) when there is, in fact,
    zero real information behind that. Never implies universal crawl
    coverage - states plainly what happened and, where actionable, what to
    do about it (Part 4/5: this tells the reader whether anything here is
    actionable, e.g. asking the merchant to allowlist this tool, versus a
    hard block this tool deliberately will not try to defeat)."""
    if not site_map.crawl_totally_failed:
        return None
    if site_map.robots_disallowed:
        return (
            "## This Audit Could Not Run\n\n"
            "This site's `robots.txt` disallows automated access to the homepage, so this tool did not "
            "attempt to crawl it. **No finding in this report reflects a real assessment of this store** - "
            "there is simply no audit to show."
        )
    homepage = site_map.pages[0] if site_map.pages else None
    category = homepage.failure_category if homepage else "unknown"
    label = FAILURE_CATEGORY_LABELS.get(category or "unknown", FAILURE_CATEGORY_LABELS["unknown"])
    recommendation = FAILURE_CATEGORY_RECOMMENDATIONS.get(category or "unknown", FAILURE_CATEGORY_RECOMMENDATIONS["unknown"])
    return (
        "## This Audit Could Not Run\n\n"
        f"This tool could not successfully fetch a single page on this site during this audit "
        f"({len(site_map.pages)} attempt(s), homepage included) - {label}. "
        "**No finding in this report reflects a real assessment of this store's compliance** - this is an "
        f"incomplete crawl, not a clean bill of health. {recommendation}"
    )


def _cannot_verify_breakdown_sentence(pages: list[CrawledPage]) -> str | None:
    """One sentence breaking down *why* each cannot-verify page couldn't be
    checked, by category (Part 4.1) - e.g. "2 bot-blocked, 1 rate-limited" -
    instead of a single opaque count."""
    counts = Counter(p.failure_category or "unknown" for p in pages)
    if not counts:
        return None
    parts = [f"{n} {FAILURE_CATEGORY_SHORT_LABELS.get(cat, cat)}" for cat, n in counts.most_common()]
    return ", ".join(parts)


# --- Prose executive summary + Final Assessment (Part 4.1, 4.4) ----------

def _prose_executive_summary(site_map: SiteMap, suspension_findings: list[Finding], other_findings: list[Finding], risk: RiskScore) -> str:
    host = urlparse(site_map.base_url).netloc or site_map.base_url
    # risk.not_applicable is the single source of truth for "this audit
    # didn't produce a real assessment" (set once by compute_risk_score) -
    # checked here instead of re-deriving from site_map.crawl_totally_failed
    # separately, which is exactly how the At a Glance section fell out of
    # sync with this one and Final Assessment previously.
    if risk.not_applicable:
        return (
            f"This audit of {host} could not be completed - see \"This Audit Could Not Run\" above for why. "
            "No risk rating is meaningful for an audit that could not assess the store."
        )
    if suspension_findings:
        critical_count = sum(1 for f in suspension_findings if f.severity == Severity.CRITICAL)
        crit_clause = f", including {critical_count} at Critical severity," if critical_count else ""
        # Part 1 of the follow-up round: the client's original stated use
        # case was specifically pre-ads compliance checking, so a count of
        # how many suspension-risk issues would also block paid Shopping
        # ads eligibility (not just free-listing eligibility) is surfaced
        # right alongside the headline suspension-risk count, not buried in
        # per-finding detail only.
        ads_count = sum(1 for f in suspension_findings if f.ads_eligibility_impact == AdsEligibilityImpact.ADS_AND_LISTINGS)
        ads_clause = (
            f" {ads_count} of these would also affect paid Shopping ads eligibility (not just free-listing eligibility)."
            if ads_count else ""
        )
        return (
            f"This audit of {host} found {len(suspension_findings)} issue(s){crit_clause} whose policy grounding "
            f"ties them to Google Merchant Center account-level suspension risk - the kind of problem that can "
            f"take the whole account offline, not just a single listing.{ads_clause} These take priority over the "
            f"{len(other_findings)} lower-tier finding(s) also found, which are more likely to affect individual "
            f"product listings than the account as a whole. Overall risk level: **{risk.rating}**."
        )
    if other_findings:
        return (
            f"This audit of {host} found no issues whose policy grounding ties them to Google Merchant Center "
            f"account-level suspension risk. {len(other_findings)} lower-tier finding(s) were found that may "
            f"still affect individual listing approval or overall quality, but nothing here points to whole-"
            f"account risk. Overall risk level: **{risk.rating}**."
        )
    return (
        f"This audit of {host} found no suspension-risk or lower-tier findings at all in the pages checked. "
        f"Overall risk level: **{risk.rating}**."
    )


def _final_assessment(
    suspension_findings: list[Finding], cannot_verify_pages: list[CrawledPage], page_only_count: int, risk: RiskScore,
) -> str:
    lines = ["## Final Assessment", ""]
    if risk.not_applicable:
        # A risk rating computed from zero real information ("LOW, 100/100 -
        # no findings to deduct for") reads as a clean bill of health, which
        # directly contradicts the "This Audit Could Not Run" banner above
        # it. Found via live testing (robots.txt-disallowed real store).
        # Checks risk.not_applicable (set once by compute_risk_score), not
        # its own separate crawl_totally_failed lookup - see
        # _prose_executive_summary's comment for why that matters.
        lines.append("**Overall risk level: not applicable** - this audit did not complete, so no risk rating is meaningful. See \"This Audit Could Not Run\" above for why.")
        lines.append("")
        lines.append("**Single most important next action:** resolve why this site could not be crawled (see above), then re-run the audit.")
        lines.append("")
        lines.append("**Limitations:** this entire audit is incomplete - nothing below reflects a real assessment of this store.")
        return "\n".join(lines)
    lines.append(f"**Overall risk level: {risk.rating}** (Internal Audit Score: {risk.score}/100). {_RISK_SCORE_DISCLAIMER}")
    lines.append("")
    if suspension_findings:
        top = min(suspension_findings, key=lambda f: _SEVERITY_ORDER.index(f.severity))
        fix_clause = f" {sanitize_for_report(top.recommended_fix)}" if top.recommended_fix else ""
        lines.append(f"**Single most important next action:** resolve \"{sanitize_for_report(top.title)}\".{fix_clause}")
    else:
        lines.append("**Single most important next action:** none of the confirmed suspension-risk kind - work through the lower-priority findings next to improve overall listing quality.")
    lines.append("")
    limitations = []
    if cannot_verify_pages:
        breakdown = _cannot_verify_breakdown_sentence(cannot_verify_pages)
        breakdown_clause = f" ({breakdown})" if breakdown else ""
        limitations.append(f"{len(cannot_verify_pages)} page(s) could not be verified during this crawl{breakdown_clause} and are not reflected in the findings above.")
        actionable = {p.failure_category for p in cannot_verify_pages if p.failure_category in ("bot_blocked", "captcha_blocked", "rate_limited")}
        for category in actionable:
            limitations.append(FAILURE_CATEGORY_RECOMMENDATIONS[category])
    if page_only_count:
        limitations.append(f"{page_only_count} finding(s) are best-effort/page-only rather than API-verified against a platform data source.")
    lines.append("**Limitations:** " + (" ".join(limitations) if limitations else "none noted for this run."))
    return "\n".join(lines)


def generate_markdown_report(
    platform: PlatformDetectionResult, site_map: SiteMap, findings: list[Finding],
    cache_stats: tuple[int, int] | None = None, major_only: bool = False,
    llm_coverage: LLMCoverageStats | None = None,
) -> str:
    """cache_stats, if given, is (hits, misses) from the LLMCache used this
    run - surfaced as a real cost/speed number (hardening round, section
    1.4), not just an internal optimization.

    major_only=True renders only findings that could put the store's GMC
    account at risk of suspension (see is_suspension_risk_finding) - every
    other finding still exists in `findings`, it's just not itemized, with a
    note stating how many were hidden so nothing is silently dropped without
    a trace.

    llm_coverage (app.llm.checks.run_llm_checks's second return value), if
    given, surfaces how much of the catalog check_editorial_quality/
    check_prohibited_content actually graded versus how much exists - a
    follow-up round fix. None (the default) renders no coverage line at
    all, for any caller that doesn't have this (e.g. a test constructing
    findings directly) - it does NOT render a misleadingly-confident "100%
    checked" claim in that case.
    """
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    suspension_findings = [f for f in findings if is_suspension_risk_finding(f)]
    other_findings = [f for f in findings if not is_suspension_risk_finding(f)]
    hidden_count = len(other_findings) if major_only else 0

    all_findings_by_page: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        if f.page_url:
            all_findings_by_page[f.page_url].append(f)

    by_severity: dict[Severity, list[Finding]] = defaultdict(list)
    for f in findings:
        by_severity[f.severity].append(f)

    reachable_pages = [p for p in site_map.pages if p.reachable]
    cannot_verify_pages = [p for p in site_map.pages if p.cannot_verify]
    broken_pages = [p for p in site_map.pages if not p.reachable and not p.cannot_verify]
    total_ssrf_validated = sum(p.ssrf_requests_validated for p in site_map.pages)
    total_ssrf_blocked = sum(p.ssrf_requests_blocked for p in site_map.pages)

    api_verified_count = sum(1 for f in findings if f.verification_method == VerificationMethod.API_VERIFIED)
    page_only_count = len(findings) - api_verified_count

    # Computed once, with the crawl-failure flag baked in - risk.not_applicable
    # is then the single source of truth every renderer below checks (At a
    # Glance, Executive Summary, Final Assessment), instead of each one
    # re-deriving "did the crawl fail" from site_map separately. That
    # duplication is exactly how At a Glance previously fell out of sync
    # with Final Assessment after only the latter was fixed.
    risk = compute_risk_score(findings, crawl_totally_failed=site_map.crawl_totally_failed)

    sections: list[str] = []

    # --- Header ---
    header = f"# GMC Compliance Audit Report\n\n**Site:** {site_map.base_url}  \n**Platform:** {platform.platform.value}  \n**Generated:** {generated_at}"
    if major_only:
        header += (
            f"  \n**Showing suspension-risk issues only** (Critical-severity findings, plus any "
            f"finding whose policy grounding ties it to GMC account suspension - see Impact: in "
            f"each finding). {hidden_count} lower-priority finding(s) not shown here; see the "
            f"full report for those."
        )
    sections.append(header)

    # --- Honest total-crawl-failure banner (Part 4) - always shown, even
    # under major_only, since a total crawl failure is not a lower-priority
    # finding to hide. ---
    failure_banner = _crawl_failure_banner(site_map)
    if failure_banner:
        sections.append(failure_banner)

    # --- API-verification gap recommendation (Part 5) - prominent, near the top ---
    # Suppressed for a totally-failed crawl: platform detection runs
    # independently (httpx REST probes) and can produce a false-positive
    # platform guess purely from a WAF 403'ing every path uniformly, not a
    # real WooCommerce signal - found live (a real non-WooCommerce store
    # whose crawl failed entirely still showed a WooCommerce-specific
    # recommendation, alongside "This Audit Could Not Run" immediately
    # above it - the same "don't assert something confident from an
    # incomplete picture" principle as the rest of this section).
    api_recommendation = None if risk.not_applicable else _api_verification_recommendation(platform, findings)
    if api_recommendation:
        sections.append(api_recommendation)

    # --- Prose executive summary (Part 4.1) ---
    sections.append("## Executive Summary\n\n" + _prose_executive_summary(site_map, suspension_findings, other_findings, risk))

    # --- Supporting stats (kept as detail, not instead of the narrative above) ---
    stats_lines = [
        "### At a Glance",
        "",
        f"**{len(suspension_findings)} issue(s) found that could put this store's Google Merchant Center account at risk of suspension.**",
        "",
        f"- Platform detected: {platform.platform.value} ({'; '.join(platform.evidence[-1:])})",
        f"- Pages crawled: {len(site_map.pages)} ({len(reachable_pages)} reachable, {len(broken_pages)} broken/404, {len(cannot_verify_pages)} could not be verified)",
        f"- Total findings: {len(findings)} ({api_verified_count} API-verified against a platform data source, {page_only_count} best-effort/page-only)",
    ]
    cannot_verify_breakdown = _cannot_verify_breakdown_sentence(cannot_verify_pages)
    if cannot_verify_breakdown:
        stats_lines.append(f"  - Could-not-verify breakdown: {cannot_verify_breakdown}")
    if major_only:
        stats_lines.append(f"- Suspension-risk findings: {len(suspension_findings)} - {hidden_count} lower-priority finding(s) omitted from this view")
    if cache_stats is not None:
        hits, misses = cache_stats
        total_calls = hits + misses
        hit_rate = f"{hits / total_calls:.0%}" if total_calls else "n/a"
        stats_lines.append(f"- LLM/vision cache: {hits} hit(s), {misses} fresh API call(s) ({hit_rate} served from cache)")
    coverage_line = _llm_coverage_sentence(llm_coverage)
    if coverage_line:
        stats_lines.append(f"- {coverage_line}")
    stats_lines.append(
        f"- SSRF guard: all three layers active on every one of {len(site_map.pages)}/{len(site_map.pages)} "
        f"page fetch attempt(s) - upfront validation, per-request interception, and a post-navigation "
        f"final-URL check, no exceptions - {total_ssrf_validated} request(s) validated and "
        f"{total_ssrf_blocked} blocked, including requests from pages that ultimately failed to load "
        f"(a request is validated the moment its destination passes the check, whether or not that "
        f"request then completes - see the Page-by-Page section for which pages didn't)"
    )
    for sev in _SEVERITY_ORDER:
        if major_only and sev != Severity.CRITICAL and not any(f.severity == sev for f in suspension_findings):
            continue
        stats_lines.append(f"  - {_SEVERITY_LABEL[sev]}: {len(by_severity[sev])}")
    stats_lines.append("")
    # risk.not_applicable is the single source of truth here too - the bug
    # this round fixes was exactly this line rendering "100/100 (LOW risk)"
    # unconditionally, contradicting the "This Audit Could Not Run" banner
    # and Final Assessment above/below it, because this was the one
    # remaining display site that never got the crawl_totally_failed check
    # the others did. Reading it off `risk` instead of re-deriving it here
    # is what makes that class of gap structurally impossible going forward.
    if risk.not_applicable:
        stats_lines.append("**Internal Audit Score: not applicable** - this audit did not complete.")
    else:
        stats_lines.append(f"**Internal Audit Score: {risk.score}/100 ({risk.rating} risk)**")
        stats_lines.append(f"_{_RISK_SCORE_DISCLAIMER}_")
    stats_lines.append("")
    stats_lines.append("Score breakdown:")
    for line in risk.breakdown:
        stats_lines.append(f"  - {line}")
    sections.append("\n".join(stats_lines))

    # --- Suspension Risk Findings (primary section, richer per-finding format) ---
    susp_header = "## Suspension Risk Findings"
    if suspension_findings:
        body = "\n\n".join(_format_finding_rich(f) for f in suspension_findings)
        sections.append(f"{susp_header}\n\n{body}")
    else:
        sections.append(f"{susp_header}\n\n_None found._")

    # --- Policy-by-Policy Review matrix (Part 4.2) ---
    sections.append("\n".join(_build_policy_matrix(findings, crawl_totally_failed=risk.not_applicable, llm_coverage=llm_coverage)))

    # --- Other Findings (secondary, deprioritized but never dropped) ---
    if not major_only:
        other_lines = ["## Other Findings (Lower Priority)", ""]
        if not other_findings:
            other_lines.append("_None - every finding in this audit is a suspension-risk finding, see above._")
        for tier in _TIER_ORDER:
            tier_findings = [f for f in other_findings if f.impact_tier == tier]
            if not tier_findings:
                continue
            other_lines.append(f"### {_TIER_LABEL[tier]}")
            other_lines.append("")
            other_lines.append("\n\n".join(_format_finding(f) for f in tier_findings))
            other_lines.append("")
        sections.append("\n".join(other_lines))
    elif hidden_count:
        sections.append(f"## Other Findings (Lower Priority)\n\n_{hidden_count} finding(s) exist but are not itemized in this suspension-risk-only view - see the full report._")

    # --- Page-by-page findings: Store Overview per-page, Catalog grouped ---
    overview_pages = [p for p in site_map.pages if not is_catalog_page(p)]
    catalog_pages = [p for p in site_map.pages if is_catalog_page(p)]

    page_lines = ["## Page-by-Page Findings", "", "### Store Overview", ""]
    page_lines.extend(_page_by_page_block(overview_pages, all_findings_by_page, major_only))
    page_lines.extend(_catalog_section_lines(catalog_pages, all_findings_by_page, major_only))
    sections.append("\n".join(page_lines))

    # --- Required fixes, prioritized (used by both the full and delta report) ---
    # Strictly the same set as suspension_findings (above) - not "or severity
    # in (CRITICAL, HIGH)", a leftover from the pre-tier severity-only design
    # that caused a real, live-found contradiction: a HIGH-severity but
    # non-suspension-tier finding (llm_policy_substance_privacy_policy, before
    # that check_id family was classified - see app/impact_tier.py) appeared
    # here while the same page showed "no suspension-risk issues found" in
    # the page-by-page section above. Both sections must draw from the exact
    # same predicate so a finding can never be "no issue" and "required fix"
    # in the same report.
    priority_findings = sorted(suspension_findings, key=lambda f: _SEVERITY_ORDER.index(f.severity))

    fix_lines = ["## Required Fixes (Prioritized)", ""]
    seen_fixes: set[tuple[str, str]] = set()
    fix_number = 1
    for f in priority_findings:
        if not f.recommended_fix:
            continue
        key = (f.check_id, f.recommended_fix)
        if key in seen_fixes:
            continue
        seen_fixes.add(key)
        where = f" ({f.page_url})" if f.page_url else ""
        risk_tag = " [GMC suspension risk]"
        fix_lines.append(f"{fix_number}. [{f.severity.value}]{risk_tag} {f.recommended_fix}{where}")
        fix_number += 1
    if fix_number == 1:
        fix_lines.append("_No critical/high-priority fixes required._")
    sections.append("\n".join(fix_lines))

    # --- Final Assessment (Part 4.4) ---
    sections.append(_final_assessment(suspension_findings, cannot_verify_pages, page_only_count, risk))

    return "\n\n".join(sections) + "\n"


def _finding_key(f: Finding) -> tuple[str, str]:
    """Stable identity for "is this the same finding across two runs" - the
    check plus the page it's about. Evidence text is allowed to change (a
    price, a wording) without that counting as a different finding.
    """
    return (f.check_id, f.page_url or "")


def generate_delta_report(
    platform: PlatformDetectionResult,
    site_map: SiteMap,
    previous_findings: list[Finding],
    current_findings: list[Finding],
    major_only: bool = False,
) -> str:
    """Compiles what changed since the last run: new issues, resolved
    issues, issues whose evidence/severity changed, and a count of what's
    unchanged. Reuses `_format_finding` so delta findings look identical to
    full-report findings.

    major_only=True restricts New/Resolved/Changed to suspension-risk
    findings (see is_suspension_risk_finding) - same definition as the full
    report's major_only, for consistency.
    """
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    prev_by_key = {_finding_key(f): f for f in previous_findings}
    curr_by_key = {_finding_key(f): f for f in current_findings}

    new_keys = curr_by_key.keys() - prev_by_key.keys()
    resolved_keys = prev_by_key.keys() - curr_by_key.keys()
    common_keys = curr_by_key.keys() & prev_by_key.keys()
    changed_keys = {
        k for k in common_keys
        if prev_by_key[k].evidence != curr_by_key[k].evidence or prev_by_key[k].severity != curr_by_key[k].severity
    }
    unchanged_keys = common_keys - changed_keys

    if major_only:
        new_keys = {k for k in new_keys if is_suspension_risk_finding(curr_by_key[k])}
        resolved_keys = {k for k in resolved_keys if is_suspension_risk_finding(prev_by_key[k])}
        changed_keys = {k for k in changed_keys if is_suspension_risk_finding(curr_by_key[k])}

    sections: list[str] = [
        f"# GMC Compliance Delta Report\n\n**Site:** {site_map.base_url}  \n**Platform:** {platform.platform.value}  \n**Generated:** {generated_at}"
        + ("  \n**Showing suspension-risk issues only**." if major_only else "")
    ]

    summary_lines = [
        "## Summary",
        "",
        f"- New issues: {len(new_keys)}",
        f"- Resolved issues: {len(resolved_keys)}",
        f"- Changed issues: {len(changed_keys)}",
        f"- Unchanged issues: {len(unchanged_keys)}",
    ]
    sections.append("\n".join(summary_lines))

    if new_keys:
        body = "\n\n".join(_format_finding(curr_by_key[k]) for k in sorted(new_keys))
        sections.append(f"## New Issues\n\n{body}")
    else:
        sections.append("## New Issues\n\n_None._")

    if resolved_keys:
        lines = ["## Resolved Issues", ""]
        for k in sorted(resolved_keys):
            f = prev_by_key[k]
            where = f" ({f.page_url})" if f.page_url else ""
            lines.append(f"- **{sanitize_for_report(f.title)}**{where} - no longer detected as of this run.")
            lines.append(f"  - Was at: {sanitize_for_report(f.location) or 'page-level'} (last detected {_format_timestamp(f.detected_at)})")
        sections.append("\n".join(lines))
    else:
        sections.append("## Resolved Issues\n\n_None._")

    if changed_keys:
        lines = ["## Changed Issues", ""]
        for k in sorted(changed_keys):
            prev_f, curr_f = prev_by_key[k], curr_by_key[k]
            where = f" ({curr_f.page_url})" if curr_f.page_url else ""
            lines.append(f"- **{sanitize_for_report(curr_f.title)}**{where}")
            lines.append(f"  - Location: {sanitize_for_report(curr_f.location) or 'page-level'}")
            lines.append(f"  - Before ({_format_timestamp(prev_f.detected_at)}): [{prev_f.severity.value}] {sanitize_for_report(prev_f.evidence)}")
            lines.append(f"  - Now ({_format_timestamp(curr_f.detected_at)}): [{curr_f.severity.value}] {sanitize_for_report(curr_f.evidence)}")
        sections.append("\n".join(lines))
    else:
        sections.append("## Changed Issues\n\n_None._")

    return "\n\n".join(sections) + "\n"
