"""Follow-up round, Part 1: ads-eligibility tagging - real research this
round found that free listings reuse the same product-data feed and are
subject to the same core Shopping ads policy categories (Google's own
"Free listings policies" page mirrors prohibited-content/misrepresentation/
editorial-quality category-for-category; "Free listings for products"
explicitly requires shipping settings and return-policy info for free-
listing eligibility, not just for ads) - see app/ads_eligibility.py's
module docstring for the full research trail and real quotes."""
from app.ads_eligibility import ads_eligibility_impact_for_finding, apply_ads_eligibility_impact
from app.impact_tier import tier_for_check_id
from app.models import AdsEligibilityImpact, Confidence, Finding, Severity


def _finding(check_id: str, title: str = "title", page_url: str | None = "https://shop.example/") -> Finding:
    return Finding(
        check_id=check_id, title=title, severity=Severity.HIGH, confidence=Confidence.CONFIRMED,
        page_url=page_url, evidence="evidence text", impact_tier=tier_for_check_id(check_id),
    )


def test_grounded_policy_area_check_ids_are_ads_and_listings():
    """Every check_id that attributes to one of the 8 tracked policy areas
    is grounded as ads_and_listings - the real research finding, not a lazy
    default (see the module docstring's citations)."""
    for check_id in (
        "business_identity_present", "llm_prohibited_content", "llm_editorial_quality",
        "woocommerce_price_mismatch",  # shipping_policy area
    ):
        result = ads_eligibility_impact_for_finding(_finding(check_id))
        assert result == AdsEligibilityImpact.ADS_AND_LISTINGS, check_id


def test_required_page_present_is_ads_and_listings_via_title_attribution():
    """required_page_present's policy area comes from the finding's title
    text (app.impact_tier.policy_area_for_finding), not the check_id alone -
    confirm that dynamic attribution still resolves to ads_and_listings."""
    f = _finding("required_page_present", title="Missing required page: Shipping policy")
    assert ads_eligibility_impact_for_finding(f) == AdsEligibilityImpact.ADS_AND_LISTINGS


def test_llm_policy_substance_dynamic_check_id_is_ads_and_listings():
    f = _finding("llm_policy_substance_privacy_policy")
    assert ads_eligibility_impact_for_finding(f) == AdsEligibilityImpact.ADS_AND_LISTINGS


def test_check_id_with_no_policy_area_attribution_is_unclear():
    """A crawl-hygiene check (not really a "policy compliance" finding in
    the GMC content sense) has no real policy text to ground an
    ads-vs-listings classification against either way."""
    for check_id in ("broken_internal_link", "https_enforced", "duplicate_nav_footer_block"):
        result = ads_eligibility_impact_for_finding(_finding(check_id))
        assert result == AdsEligibilityImpact.UNCLEAR, check_id


def test_apply_ads_eligibility_impact_does_not_mutate_input():
    original = _finding("llm_prohibited_content")
    assert original.ads_eligibility_impact == AdsEligibilityImpact.UNCLEAR  # construction-time default
    updated = apply_ads_eligibility_impact([original])
    assert original.ads_eligibility_impact == AdsEligibilityImpact.UNCLEAR  # unchanged
    assert updated[0].ads_eligibility_impact == AdsEligibilityImpact.ADS_AND_LISTINGS
