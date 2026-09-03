"""Grounds every check_id's ImpactTier in the real GMC policy text retrieved
from the Phase C RAG index (app/llm/policy_rag.py), not an assumption dressed
up as a lookup.

Method: for each of the 8 indexed policy areas, `get_policy_context` was
queried live with "what happens if a merchant violates this policy - is the
individual product disapproved, or can the account be suspended?" and the
real retrieved chunks (with their source URLs and section headings) were
read to decide the tier. The exact retrieved passages relied on for each
policy area are quoted in _POLICY_AREA_GROUNDING below, so every assignment
below can be traced back to real, live-fetched GMC Help Center text rather
than a guess. See validation/impact_tier_grounding.md for the full retrieved
text this was derived from (same live index, same query, reproducible).

Findings from checks with no policy area that confidently covers them
(mostly per-item product-data/image mechanics: price/stock/image checks
with no suspension language found anywhere) default to LISTING_DISAPPROVAL
per the explicit instruction to default ambiguous cases there rather than
downgrade them to QUALITY_IMPROVEMENT on a guess - see ImpactTier's
docstring in app/models.py.

Watch for dynamic check_id families: app.llm.checks.check_policy_page_substance
produces check_id=f"llm_policy_substance_{policy_id}" (one per required
policy page type), not a single static string. The first pass at this
module missed that entirely - those check_ids silently fell through to the
LISTING_DISAPPROVAL default, and a real audit produced a HIGH-severity
"lacks required substance" finding that never showed up as suspension-risk
anywhere, contradicting the Required Fixes list that (incorrectly, at the
time) still surfaced it by severity alone. Both are fixed now: the 4
concrete llm_policy_substance_* check_ids are classified explicitly below,
and Required Fixes (app/report.py) uses tier alone, not severity, to
decide inclusion - see that module for the second half of the fix.
"""
from __future__ import annotations

from app.models import Finding, ImpactTier

# Direct quotes from the real retrieved policy text, kept here so every
# tier assignment below is traceable to something a human can verify against
# the live GMC Help Center page cited alongside it.
_POLICY_AREA_GROUNDING: dict[str, str] = {
    "misrepresentation": (
        'support.google.com/merchants/answer/6150127 ("Our policy"): "If violations of this '
        "policy are found, your Google accounts will be suspended upon detection and without "
        'prior warning." Its "Best practices" section explicitly names business description, '
        "contact information, and own-branding consistency, and its \"Unavailable offers\" "
        "section carries the same suspend-without-warning consequence - covers business-identity "
        "consistency, external-domain links, missing required pages, and non-functional "
        "checkout/cart."
    ),
    "prohibited_content": (
        'support.google.com/merchants/answer/6149970 ("Overview..."): enforcement includes '
        '"disapproving violating Shopping ads", but explicitly extends to "suspending accounts '
        'for repeat or egregious violations" - prohibited content has historically been treated '
        "as the more severe class GMC's own enforcement page names alongside suspension."
    ),
    "shipping_policy": (
        'support.google.com/merchants/answer/6324484 ("Minimum requirements"/"Best practices"): '
        '"we\'ll disapprove your product" / "your product or account could be disapproved" - '
        "framed as item-level product-data accuracy, not an account-suspension policy. Applied "
        "here to the same class of per-item pricing/availability-accuracy checks generally "
        "(GMC's separate product-data-specification policy isn't in this project's 8-area RAG "
        "index, so this is the closest indexed grounding for that class of check)."
    ),
    "editorial_quality": (
        'support.google.com/merchants/answer/12079604 ("What you can do"/"Examples of what\'s '
        'not allowed"): "Products that don\'t comply... may be disapproved" - no suspension '
        "language anywhere in the retrieved text for thin/duplicate content quality issues."
    ),
    "policies_general": (
        'support.google.com/merchants/answer/13693195 ("Account suspension caused by policy '
        'violations"): before a suspension re-review you must verify "the accuracy and '
        'consistency of product details, business information, policies, and contact '
        'information" - missing/inconsistent required policy pages sit in the same '
        "trust-signal cluster reviewed for suspension, alongside business identity."
    ),
}

# check_id -> (tier, which _POLICY_AREA_GROUNDING key backs it, short note for
# check_ids that needed a judgment call beyond a direct quote).
_CLASSIFICATION: dict[str, tuple[ImpactTier, str, str]] = {
    # --- Suspension risk: site-wide trust-signal / misrepresentation-shaped ---
    "business_identity_present": (ImpactTier.SUSPENSION_RISK, "misrepresentation", "no business contact info at all"),
    "business_identity_email_consistency": (ImpactTier.SUSPENSION_RISK, "misrepresentation", "inconsistent contact info across the site"),
    "business_identity_phone_consistency": (ImpactTier.SUSPENSION_RISK, "misrepresentation", "inconsistent contact info across the site"),
    "business_identity_phone_country_mismatch": (ImpactTier.SUSPENSION_RISK, "misrepresentation", "inconsistent contact info across the site"),
    "required_page_present": (ImpactTier.SUSPENSION_RISK, "policies_general", "a missing required policy/contact page is one of the trust signals suspension reviews check"),
    "purchase_journey_cart_unreachable": (ImpactTier.SUSPENSION_RISK, "misrepresentation", "matches 'Unavailable offers' - a cart that can't be reached makes the offer unavailable"),
    "purchase_journey_checkout_unreachable": (ImpactTier.SUSPENSION_RISK, "misrepresentation", "matches 'Unavailable offers'"),
    "purchase_journey_add_to_cart_failed": (ImpactTier.SUSPENSION_RISK, "misrepresentation", "matches 'Unavailable offers' if the control is genuinely missing, not just unrecognized"),
    "contact_form_missing_field": (ImpactTier.SUSPENSION_RISK, "misrepresentation", "a contact form that can't actually collect a name/email/message is functionally 'not reachable through the contact information provided'"),
    "form_action_unreachable": (ImpactTier.SUSPENSION_RISK, "misrepresentation", "a form whose submission endpoint 404s/errors is functionally unreachable, same reasoning as contact_form_missing_field"),
    "llm_prohibited_content": (ImpactTier.SUSPENSION_RISK, "prohibited_content", "explicit policy category, escalates to account suspension on repeat/egregious violations"),
    "llm_image_prohibited_content_flag": (ImpactTier.SUSPENSION_RISK, "prohibited_content", "same as llm_prohibited_content, image-based"),
    "llm_claim_policy_contradiction": (ImpactTier.SUSPENSION_RISK, "misrepresentation", "a claim contradicting the store's own stated policy is the same category of issue as an inconsistent business-identity field, already grounded via misrepresentation"),
    # Dynamic check_id family (app/llm/checks.py's check_policy_page_substance
    # appends the policy_id, one call site per required policy page type) -
    # a policy page that EXISTS but lacks real substance is functionally the
    # same trust-signal gap as the page being missing entirely, so it's
    # grounded the same way as required_page_present: the same
    # "policies_general" suspension-review criterion applies regardless of
    # whether the gap is "no page" or "a page with nothing useful on it".
    "llm_policy_substance_privacy_policy": (ImpactTier.SUSPENSION_RISK, "policies_general", "a privacy policy page that exists but lacks real substance is functionally near-equivalent to a missing one"),
    "llm_policy_substance_shipping_policy": (ImpactTier.SUSPENSION_RISK, "policies_general", "a shipping policy page that exists but lacks real substance is functionally near-equivalent to a missing one"),
    "llm_policy_substance_returns_refunds": (ImpactTier.SUSPENSION_RISK, "policies_general", "a returns policy page that exists but lacks real substance is functionally near-equivalent to a missing one"),
    "llm_policy_substance_terms_of_service": (ImpactTier.SUSPENSION_RISK, "policies_general", "a terms-of-service page that exists but lacks real substance is functionally near-equivalent to a missing one"),

    # --- Listing disapproval: per-item product-data/pricing accuracy ---
    "generic_product_price_missing": (ImpactTier.LISTING_DISAPPROVAL, "shipping_policy", "per-item product-data accuracy"),
    "generic_product_availability_missing": (ImpactTier.LISTING_DISAPPROVAL, "shipping_policy", "per-item product-data accuracy"),
    "woocommerce_price_mismatch": (ImpactTier.LISTING_DISAPPROVAL, "shipping_policy", "per-item product-data accuracy"),
    "woocommerce_stock_mismatch": (ImpactTier.LISTING_DISAPPROVAL, "shipping_policy", "per-item product-data accuracy"),
    "shopify_price_mismatch": (ImpactTier.LISTING_DISAPPROVAL, "shipping_policy", "per-item product-data accuracy"),
    "shopify_availability_mismatch": (ImpactTier.LISTING_DISAPPROVAL, "shipping_policy", "per-item product-data accuracy"),
    "purchase_journey_cart_price_mismatch": (ImpactTier.LISTING_DISAPPROVAL, "shipping_policy", "price honored through checkout - a pricing-accuracy issue"),
    "purchase_journey_cart_price_not_found": (ImpactTier.LISTING_DISAPPROVAL, "shipping_policy", "pricing-accuracy adjacent, unverified"),
    "purchase_journey_shipping_not_shown": (ImpactTier.LISTING_DISAPPROVAL, "shipping_policy", "shipping-cost disclosure accuracy"),
    "purchase_journey_total_lower_than_product_price": (ImpactTier.LISTING_DISAPPROVAL, "shipping_policy", "pricing-accuracy issue, may be a legitimate discount"),
    "product_image_broken": (ImpactTier.LISTING_DISAPPROVAL, "", "no suspension language found for image mechanics; brief's own default"),
    "product_image_low_resolution": (ImpactTier.LISTING_DISAPPROVAL, "", "no suspension language found for image mechanics; brief's own default"),
    "product_image_missing_alt_text": (ImpactTier.LISTING_DISAPPROVAL, "", "no suspension language found for image mechanics; brief's own default"),
    "product_image_placeholder_filename": (ImpactTier.LISTING_DISAPPROVAL, "", "no suspension language found for image mechanics; brief's own default"),
    "llm_image_product_mismatch": (ImpactTier.LISTING_DISAPPROVAL, "", "no direct textual grounding found; flagged ambiguous, defaulted per instructions"),
    "external_domain_link": (
        ImpactTier.LISTING_DISAPPROVAL, "",
        "reconsidered from the brief's own starting hypothesis: misrepresentation's 'own branding' "
        "best practice is about site-wide identity/branding consistency, a pattern signal - this "
        "check fires once per individual external link found (including entirely benign ones, e.g. "
        "a social-media footer icon), so tagging every instance as suspension-risk would be an "
        "alarmist mismatch between a per-link finding and a site-wide policy concern; flagged "
        "ambiguous, defaulted per instructions rather than over-claiming",
    ),
    "llm_image_vision_check": (ImpactTier.LISTING_DISAPPROVAL, "", "diagnostic 'could not evaluate' placeholder, always CANNOT_VERIFY - not a confirmed violation"),
    "purchase_journey_blocked_ssrf": (ImpactTier.LISTING_DISAPPROVAL, "", "our own crawler declining to proceed, not a confirmed real-customer-facing failure; always CANNOT_VERIFY"),
    "broken_internal_link": (ImpactTier.LISTING_DISAPPROVAL, "", "no direct textual grounding found; flagged ambiguous, defaulted per instructions"),
    "broken_image": (ImpactTier.LISTING_DISAPPROVAL, "", "no direct textual grounding found; flagged ambiguous, defaulted per instructions"),
    "https_enforced": (ImpactTier.LISTING_DISAPPROVAL, "", "no direct textual grounding found; flagged ambiguous, defaulted per instructions"),
    "https_mixed_content_link": (ImpactTier.LISTING_DISAPPROVAL, "", "no direct textual grounding found; flagged ambiguous, defaulted per instructions"),
    "duplicate_nav_footer_block": (ImpactTier.LISTING_DISAPPROVAL, "", "crawl-noise artifact, not a policy issue; flagged ambiguous, defaulted per instructions"),
    "form_email_field_weak_validation": (ImpactTier.LISTING_DISAPPROVAL, "", "minor UX/data-quality heuristic, not a reachability issue; flagged ambiguous, defaulted per instructions"),

    # --- Quality improvement: no enforcement action tied to it in the retrieved text ---
    "llm_editorial_quality": (ImpactTier.QUALITY_IMPROVEMENT, "editorial_quality", "retrieved text is disapproval-only for item content quality, no suspension language"),
    "duplicate_product_listing": (ImpactTier.QUALITY_IMPROVEMENT, "editorial_quality", 'editorial_quality\'s own retrieved text names this exactly: "The same product details being used for multiple products without differentiation" - disapproval-only framing, no suspension language'),
}

# check_ids classified above whose grounding is explicitly a judgment call
# rather than a direct policy-text quote (the "" policy_area rows above) -
# surfaced separately so a report reader can see which assignments are
# solid vs which are the cautious default pending clearer policy text.
AMBIGUOUS_CHECK_IDS: frozenset[str] = frozenset(
    check_id for check_id, (_, policy_area, _) in _CLASSIFICATION.items() if not policy_area
)


def tier_for_check_id(check_id: str) -> ImpactTier:
    entry = _CLASSIFICATION.get(check_id)
    return entry[0] if entry else ImpactTier.LISTING_DISAPPROVAL


def citation_for_check_id(check_id: str) -> str | None:
    entry = _CLASSIFICATION.get(check_id)
    if entry is None:
        return None
    _, policy_area, note = entry
    grounding = _POLICY_AREA_GROUNDING.get(policy_area)
    if grounding is None:
        return note
    return f"{grounding} ({note})" if note else grounding


# --- Policy-area attribution (app/report.py's Policy-by-Policy Review
# matrix) - a DIFFERENT axis from the tier grounding above: which of the 8
# real GMC Help Center areas (app.policy_watcher.POLICY_SOURCE_URLS) a
# finding is ABOUT, not which policy text justified its suspension-vs-
# disapproval tier. business_identity_* checks, for example, are tier-
# grounded via the misrepresentation page's best-practices text, but belong
# in the matrix's own "Business Identity" row, not "Misrepresentation".
_STATIC_POLICY_AREA_BY_CHECK_ID: dict[str, str] = {
    "business_identity_present": "business_identity",
    "business_identity_email_consistency": "business_identity",
    "business_identity_phone_consistency": "business_identity",
    "business_identity_phone_country_mismatch": "business_identity",
    "contact_form_missing_field": "business_identity",
    "form_action_unreachable": "business_identity",
    "external_domain_link": "misrepresentation",
    "purchase_journey_cart_unreachable": "misrepresentation",
    "purchase_journey_checkout_unreachable": "misrepresentation",
    "purchase_journey_add_to_cart_failed": "misrepresentation",
    "purchase_journey_blocked_ssrf": "misrepresentation",
    "llm_prohibited_content": "prohibited_content",
    "llm_image_prohibited_content_flag": "prohibited_content",
    "llm_claim_policy_contradiction": "misrepresentation",
    "llm_editorial_quality": "editorial_quality",
    "duplicate_product_listing": "editorial_quality",
    "generic_product_price_missing": "shipping_policy",
    "generic_product_availability_missing": "shipping_policy",
    "woocommerce_price_mismatch": "shipping_policy",
    "woocommerce_stock_mismatch": "shipping_policy",
    "shopify_price_mismatch": "shipping_policy",
    "shopify_availability_mismatch": "shipping_policy",
    "purchase_journey_cart_price_mismatch": "shipping_policy",
    "purchase_journey_cart_price_not_found": "shipping_policy",
    "purchase_journey_shipping_not_shown": "shipping_policy",
    "purchase_journey_total_lower_than_product_price": "shipping_policy",
}

# required_page_present is one check_id shared across 5 page types - which
# policy area a specific finding belongs to has to come from its title text.
_REQUIRED_PAGE_TITLE_HINTS: list[tuple[str, str]] = [
    ("privacy", "privacy_policy"),
    ("shipping", "shipping_policy"),
    ("return", "returns_refunds"),
    ("refund", "returns_refunds"),
    ("terms", "terms_of_service"),
    ("contact", "business_identity"),
]


def policy_area_for_finding(f: Finding) -> str | None:
    """None means this finding isn't attributed to any of the 8 tracked
    policy areas (e.g. generic image/link mechanics) - it still exists in
    the underlying data, it just doesn't appear as a row in the Policy-by-
    Policy Review matrix.
    """
    if f.check_id.startswith("llm_policy_substance_"):
        return f.check_id.removeprefix("llm_policy_substance_")
    if f.check_id == "required_page_present":
        title_lower = f.title.lower()
        for hint, area in _REQUIRED_PAGE_TITLE_HINTS:
            if hint in title_lower:
                return area
        return None
    return _STATIC_POLICY_AREA_BY_CHECK_ID.get(f.check_id)


def apply_impact_tiers(findings: list[Finding]) -> list[Finding]:
    """Returns new Finding objects with impact_tier set from the real-policy-
    text-grounded classification above. Never mutates the input list/objects
    (Pydantic models are shared elsewhere in the pipeline, e.g. for delta
    diffing) - callers should use the returned list.
    """
    return [f.model_copy(update={"impact_tier": tier_for_check_id(f.check_id)}) for f in findings]
