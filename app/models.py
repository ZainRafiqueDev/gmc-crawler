"""Core data models shared across the pipeline."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Platform(str, Enum):
    WOOCOMMERCE = "woocommerce"
    WORDPRESS = "wordpress"
    SHOPIFY = "shopify"
    UNKNOWN = "unknown"


class PlatformDetectionResult(BaseModel):
    platform: Platform
    base_url: str
    evidence: list[str] = Field(default_factory=list)


class PageType(str, Enum):
    HOMEPAGE = "homepage"
    PRODUCT = "product"
    COLLECTION = "collection"
    CART = "cart"
    CHECKOUT = "checkout"
    PRIVACY_POLICY = "privacy_policy"
    SHIPPING_POLICY = "shipping_policy"
    RETURNS_POLICY = "returns_policy"
    TERMS_OF_SERVICE = "terms_of_service"
    CONTACT_ABOUT = "contact_about"
    FAQ = "faq"
    BLOG_OTHER = "blog_other"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Confidence(str, Enum):
    CONFIRMED = "confirmed"
    POTENTIAL_RISK = "potential_risk"
    CANNOT_VERIFY = "cannot_verify"


class CrawledPage(BaseModel):
    url: str
    page_type: PageType
    depth: int
    title: str = ""
    headings: list[str] = Field(default_factory=list)
    status: int | None = None
    reachable: bool = True
    cannot_verify: bool = False
    error: str | None = None
    internal_links: list[str] = Field(default_factory=list)
    external_links: list[str] = Field(default_factory=list)
    image_srcs: list[str] = Field(default_factory=list)
    html: str | None = None
    text: str | None = None
    # De-boilerplated text (nav/header/footer/cookie-consent stripped) -
    # same cleanup the page classifier already does, reused for LLM-graded
    # check prompts (hardening round, section 2.1) so the model isn't
    # grounded on cookie-banner/nav noise, and so fewer tokens are sent.
    # None for unreachable pages, or if fetched before this field existed.
    main_content_text: str | None = None
    # Positive, countable confirmation that all three SSRF guard layers ran
    # for this page (upfront + per-request interception + post-navigation
    # final-URL check - see app/security/ssrf_guard.py) - not just an
    # absence of blocked requests. Surfaced in the report, not only logged.
    ssrf_requests_validated: int = 0
    ssrf_requests_blocked: int = 0
    # Why this page is unreachable, one of app.fetch's FAILURE_CATEGORY_LABELS
    # keys ("not_found", "blocked_ssrf", "captcha_blocked", "bot_blocked",
    # "rate_limited", "network_error", "unknown") - None when reachable.
    # Lets checks/reporting state *why* a page couldn't be verified (broader
    # real-world crawl robustness round) instead of a generic "could not
    # verify", same spirit as the earlier DNS-hiccup honesty fix.
    failure_category: str | None = None
    # Best-effort content language for this page, from <html lang="...">,
    # normalized to a base 2-letter code (e.g. "es" from "es-MX"). None when
    # unreachable or the attribute is absent. Used to avoid a confident
    # "missing page" verdict from a classifier that only recognizes a
    # handful of languages (see app.page_classifier.SUPPORTED_LANGUAGES).
    detected_language: str | None = None


class SiteMap(BaseModel):
    base_url: str
    pages: list[CrawledPage] = Field(default_factory=list)
    sitemap_urls_found: int = 0
    # True when robots.txt disallowed crawling the homepage at all - a
    # deliberate refusal to crawl, distinct from a failed crawl attempt, and
    # reported as such rather than as a pile of confident "missing" findings.
    robots_disallowed: bool = False

    def pages_of_type(self, page_type: PageType) -> list[CrawledPage]:
        return [p for p in self.pages if p.page_type == page_type]

    @property
    def reachable_pages(self) -> list[CrawledPage]:
        return [p for p in self.pages if p.reachable]

    @property
    def crawl_totally_failed(self) -> bool:
        """True when literally nothing could be fetched - homepage included
        (or robots.txt refused the crawl outright, leaving zero pages).
        Checks that would otherwise emit a confident "missing"/"not found"
        verdict must treat this as unknown, not a confirmed negative - see
        app.checks.deterministic.check_required_pages and
        app.checks.business_identity.check_business_identity_consistency.
        """
        return not any(p.reachable for p in self.pages)


class ImpactTier(str, Enum):
    """How a finding maps onto Google's two-tier Shopping ads enforcement
    model (see app/impact_tier.py for the real-policy-text grounding behind
    every check_id's assignment): SUSPENSION_RISK for site-wide trust-signal
    problems Google's own misrepresentation/prohibited-content policy text
    ties to whole-account suspension (often without warning); LISTING_
    DISAPPROVAL for per-item data-quality problems that get an individual
    product rejected but leave the account active; QUALITY_IMPROVEMENT for
    issues Google's own text never ties to an enforcement action at all
    (e.g. editorial/content quality). Defaults to LISTING_DISAPPROVAL - the
    cautious, still-visible choice - for any check_id without a confident
    grounding, rather than guessing it's harmless.
    """
    SUSPENSION_RISK = "suspension_risk"
    LISTING_DISAPPROVAL = "listing_disapproval"
    QUALITY_IMPROVEMENT = "quality_improvement"


class AdsEligibilityImpact(str, Enum):
    """Whether a finding's underlying policy requirement is specific to
    paid Shopping ads, or also affects free-listing eligibility - the
    client's original stated use case was pre-ads compliance checking, and
    every finding previously answered one undifferentiated "is this
    GMC-compliant" question without this distinction. See
    app/ads_eligibility.py for the real-policy-text grounding behind every
    check_id's assignment, the same evidentiary standard as ImpactTier.

    Only two concrete values plus an escape hatch, deliberately not a false
    3-way ADS_ONLY/LISTINGS_ONLY/BOTH split: real research this round found
    that free listings reuse the same product-data feed and are subject to
    the same core Shopping policies as paid ads (Google's own "Free
    listings policies" page explicitly mirrors the Shopping ads prohibited-
    content/misrepresentation/editorial categories), so a "listings only"
    bucket would need real evidence of something narrower than that shared
    baseline - not found for any currently-implemented check.
    """
    ADS_AND_LISTINGS = "ads_and_listings"
    LISTINGS_ONLY = "listings_only"
    UNCLEAR = "unclear"


class VerificationMethod(str, Enum):
    """Whether a finding was cross-checked against an authoritative platform
    API (WooCommerce REST, Shopify products.json) or inferred only from what
    the rendered page shows. Both are legitimate - this just tells the
    reader how much to trust a "pass"/"cannot verify" on that finding.
    """
    API_VERIFIED = "api_verified"
    PAGE_ONLY = "page_only"


class Finding(BaseModel):
    check_id: str
    title: str
    severity: Severity
    confidence: Confidence
    page_url: str | None = None
    evidence: str
    policy_reference: str | None = None
    # The actual retrieved policy text (PolicyContext.summary) an LLM-graded
    # finding was graded against - not a new lookup, just preserving data
    # the Phase C RAG retrieval already computed at finding-creation time
    # instead of discarding it once policy_reference's citation string is
    # built. None for deterministic checks (no RAG retrieval happened) or
    # if retrieval fell back to the stub snippet.
    policy_requirement_text: str | None = None
    recommended_fix: str | None = None
    verification_method: VerificationMethod = VerificationMethod.PAGE_ONLY
    # Set automatically at construction time - i.e. when the check that found
    # it actually ran, not when the report is compiled later.
    detected_at: datetime = Field(default_factory=_utcnow)
    # Precise in-page location beyond just page_url: a CSS selector for
    # deterministic checks that parsed the element directly, or a short
    # human-readable section/element description for LLM-graded and vision
    # checks (required as part of their structured output, not free text
    # folded into the explanation). None only when the finding is inherently
    # page-level/site-wide (e.g. "this page is entirely unreachable").
    location: str | None = None
    # True if this LLM/vision finding was served from the content-hash-keyed
    # cache (app/llm/cache.py) instead of a fresh API call - never silently
    # reused without marking it, so a reader knows whether this reflects the
    # latest grading pass or a still-valid earlier one.
    from_cache: bool = False
    # Assigned post-hoc by app.impact_tier.apply_impact_tiers (not set at
    # construction time by individual checks) - see ImpactTier's docstring.
    impact_tier: ImpactTier = ImpactTier.LISTING_DISAPPROVAL
    # Assigned post-hoc by app.ads_eligibility.apply_ads_eligibility_impact,
    # same pattern as impact_tier above - see AdsEligibilityImpact's
    # docstring.
    ads_eligibility_impact: AdsEligibilityImpact = AdsEligibilityImpact.UNCLEAR
    # When the cited policy text (policy_requirement_text) was last
    # confirmed against the live GMC Help Center page - app.llm.policy_rag's
    # PolicyContext.verified_at, threaded through unchanged (not a new
    # lookup). None for deterministic checks (no RAG retrieval happened) or
    # when retrieval fell back to the stub snippet.
    policy_last_verified: datetime | None = None
    # Path to an annotated screenshot (relative to Settings.report_output_dir),
    # assigned post-hoc by app.checks.screenshot_annotator.capture_annotated_screenshots
    # - same non-mutating pattern as impact_tier/ads_eligibility_impact.
    # Only ever set for LLM-graded Suspension Risk Findings whose own
    # already-verified evidence quote could actually be located in the
    # rendered page (annotated screenshots follow-up, Part 3) - None
    # otherwise, including when a screenshot was attempted but the quote
    # couldn't be found (skipped rather than guessed, per that round's brief).
    screenshot_path: str | None = None


class LLMCoverageStats(BaseModel):
    """How much of the catalog check_editorial_quality/check_prohibited_content
    actually graded, versus how much exists - a follow-up round fix. These
    two checks sample a fixed number of product pages
    (app.llm.checks._MAX_PRODUCT_PAGES_CHECKED) regardless of catalog size,
    always the first N in crawl order (confirmed, not assumed, before this
    was built - see app.llm.checks.run_llm_checks). A "Pass" on Prohibited
    Content for a 500-product store could mean 1% coverage - read by a
    client as if it were comprehensive without this surfaced explicitly,
    the same discipline already applied to the WooCommerce-API-connection
    gap (app.report._api_verification_recommendation).
    """
    llm_configured: bool
    total_reachable_product_pages: int
    product_pages_checked: int

    @property
    def coverage_fraction(self) -> float | None:
        """None (not 0.0) when there were no product pages to check at all -
        a store with no products isn't a coverage gap, it's not applicable."""
        if self.total_reachable_product_pages == 0:
            return None
        return self.product_pages_checked / self.total_reachable_product_pages

    @property
    def is_partial(self) -> bool:
        """True only when there was real, uncovered catalog left unchecked -
        never true for a small store where the sample covered everything."""
        return self.total_reachable_product_pages > self.product_pages_checked
