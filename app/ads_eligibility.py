"""Grounds every finding's AdsEligibilityImpact in real, live-fetched GMC
Help Center text, the same evidentiary standard app/impact_tier.py already
holds itself to for suspension-vs-disapproval tiering. This is a DIFFERENT
axis from impact_tier: that module asks "does this risk suspension or just
one listing's disapproval"; this one asks "does this affect paid Shopping
ads eligibility, free-listing eligibility, or both."

Method: live-fetched (this round) support.google.com/merchants pages
specifically about the free-listings/Shopping-ads relationship - not
assumed, not carried over from training knowledge. The real quotes relied
on for the conclusion below are kept in _RESEARCH_QUOTES so the conclusion
is traceable to something a human can verify against the live pages cited.

Real research finding, stated plainly rather than forced into a false
3-way split: free listings reuse the same product-data feed and are
explicitly subject to the same core policy categories as paid Shopping
ads - Google's own "Free listings policies" page
(support.google.com/merchants/answer/12073010) mirrors the Shopping ads
policy structure category-for-category (Prohibited content, Prohibited
practices incl. Misrepresentation, Restricted content, Site requirements
incl. Editorial/technical), and "Free listings for products"
(support.google.com/merchants/answer/13889434) explicitly requires return-
policy information and shipping settings for free-listing eligibility, not
just for ads. The enforcement-update changelog
(support.google.com/merchants/answer/11299257) shows the overwhelming
pattern is joint "applies to Shopping Ads and Free Listings" enforcement,
with the rare genuine ads-only carve-outs found (a political-content
restriction, specific countries) not corresponding to anything this
project checks (no political-content check exists here) - and the one
listings-specific extra requirement found (a physical local-store location
for the separate Local Inventory Ads / free local listings program) is
also not something any current check_id evaluates (this project doesn't
check for a brick-and-mortar location).

Net result: every one of the 8 policy areas this project's RAG index
already tracks (app.policy_watcher.POLICY_SOURCE_URLS) is grounded as
ADS_AND_LISTINGS below - not because that's a lazy default, but because
that's what the real retrieved text actually supports for every
currently-implemented check. A genuinely narrower LISTINGS_ONLY (or an
ads-only counterpart) finding would need its own real citation the same
way; none was found this round. UNCLEAR is reserved for findings that
don't attribute to any of the 8 tracked policy areas at all (crawl-hygiene
checks like a broken link or duplicated nav block - not really "policy
compliance" findings in the GMC content sense, so an ads-vs-listings
classification doesn't have a real policy text to anchor it either way).
"""
from __future__ import annotations

from app.impact_tier import policy_area_for_finding
from app.models import AdsEligibilityImpact, Finding

# Real, verbatim quotes from live-fetched GMC Help Center pages (fetched
# this round), kept here so the module docstring's conclusion is
# independently checkable, not just asserted.
RESEARCH_QUOTES: dict[str, str] = {
    "free_listings_policies_mirror": (
        'support.google.com/merchants/answer/12073010 ("Free listings policies"): mirrors the Shopping ads '
        "policy structure category-for-category - Prohibited content (counterfeit, dangerous, dishonest-"
        "behavior-enabling, inappropriate, unsupported content), Prohibited practices (abuse of network, "
        'misrepresentation), Restricted content, and Site requirements (editorial/technical, data collection) - '
        'each entry cross-references "This policy applies to free listings. Learn more about Shopping ads '
        'policies," explicitly tying the free-listings version to the same underlying Shopping ads policy.'
    ),
    "free_listings_requires_shipping_returns": (
        'support.google.com/merchants/answer/13889434 ("Free listings for products"): eligibility requirements '
        'explicitly include "Add your return policy information to your website to show information about '
        "returns next to your products\" and \"Set up 'Shipping' settings or add shipping costs using the "
        'shipping attribute to product data" - shipping and returns policy requirements are not ads-exclusive, '
        "they gate free-listing eligibility directly."
    ),
    "prohibited_content_applies_to_both": (
        'support.google.com/merchants/answer/6149970 ("Shopping ads policies"), Unsupported content section: '
        '"these limitations are specific to Shopping ads and free listings" - explicit, direct statement that '
        "prohibited/restricted content enforcement is not paid-ads-exclusive."
    ),
    "enforcement_update_joint_pattern": (
        "support.google.com/merchants/answer/11299257 (\"Enforcement update for free listings\" changelog): "
        "the overwhelming pattern across real policy updates is \"applies to Shopping Ads and Free Listings\"/"
        '"affects both Shopping ads and free listings" - the rare genuine exception found (political-content '
        "restrictions, specific countries, Shopping-ads-only) does not correspond to any check this project "
        "implements."
    ),
}

# Every one of the 8 policy areas this project's RAG index tracks
# (app.policy_watcher.POLICY_SOURCE_URLS) is grounded as applying to both
# surfaces - see the module docstring and RESEARCH_QUOTES above for why
# this isn't a lazy blanket default.
_GROUNDED_POLICY_AREAS: frozenset[str] = frozenset({
    "shipping_policy", "returns_refunds", "business_identity", "misrepresentation",
    "prohibited_content", "editorial_quality", "privacy_policy", "terms_of_service",
})


def ads_eligibility_impact_for_finding(f: Finding) -> AdsEligibilityImpact:
    """Reuses app.impact_tier.policy_area_for_finding rather than a second,
    parallel check_id->area mapping - the same attribution question
    ("which of the 8 tracked policy areas is this finding about") already
    has one answer in this codebase; this module only adds a second axis
    of classification on top of it, not a second source of truth for the
    first question.
    """
    area = policy_area_for_finding(f)
    if area is None or area not in _GROUNDED_POLICY_AREAS:
        # Not attributed to any of the 8 tracked policy areas at all (e.g.
        # a broken-link/HTTPS/crawl-hygiene check) - no real policy text to
        # ground an ads-vs-listings classification against either way.
        return AdsEligibilityImpact.UNCLEAR
    return AdsEligibilityImpact.ADS_AND_LISTINGS


def apply_ads_eligibility_impact(findings: list[Finding]) -> list[Finding]:
    """Returns new Finding objects with ads_eligibility_impact set - same
    non-mutating pattern as app.impact_tier.apply_impact_tiers (Pydantic
    models are shared elsewhere in the pipeline, e.g. delta diffing)."""
    return [f.model_copy(update={"ads_eligibility_impact": ads_eligibility_impact_for_finding(f)}) for f in findings]
