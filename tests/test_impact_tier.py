from app.impact_tier import AMBIGUOUS_CHECK_IDS, apply_impact_tiers, citation_for_check_id, tier_for_check_id
from app.models import Confidence, Finding, ImpactTier, Severity


def _finding(check_id: str) -> Finding:
    return Finding(check_id=check_id, title="t", severity=Severity.HIGH, confidence=Confidence.CONFIRMED, evidence="e")


def test_misrepresentation_shaped_checks_are_suspension_risk():
    for check_id in ["business_identity_present", "business_identity_phone_consistency", "required_page_present"]:
        assert tier_for_check_id(check_id) == ImpactTier.SUSPENSION_RISK


def test_external_domain_link_is_listing_disapproval_not_suspension_risk():
    # Reconsidered from the brief's own starting hypothesis - see
    # app/impact_tier.py and validation/impact_tier_grounding.md for why:
    # this check fires per-individual-link, not as a site-wide pattern
    # signal, so blanket-suspension-tagging every link would overclaim.
    assert tier_for_check_id("external_domain_link") == ImpactTier.LISTING_DISAPPROVAL
    assert "external_domain_link" in AMBIGUOUS_CHECK_IDS


def test_prohibited_content_is_suspension_risk():
    assert tier_for_check_id("llm_prohibited_content") == ImpactTier.SUSPENSION_RISK
    assert tier_for_check_id("llm_image_prohibited_content_flag") == ImpactTier.SUSPENSION_RISK


def test_broken_form_checks_are_suspension_risk():
    assert tier_for_check_id("contact_form_missing_field") == ImpactTier.SUSPENSION_RISK
    assert tier_for_check_id("form_action_unreachable") == ImpactTier.SUSPENSION_RISK


def test_claim_policy_contradiction_is_suspension_risk_via_misrepresentation():
    """Follow-up round, Part 3: a claim contradicting the store's own
    stated policy is grounded the same way as inconsistent business-
    identity fields."""
    from app.impact_tier import policy_area_for_finding

    assert tier_for_check_id("llm_claim_policy_contradiction") == ImpactTier.SUSPENSION_RISK
    assert policy_area_for_finding(_finding("llm_claim_policy_contradiction")) == "misrepresentation"


def test_form_validation_heuristic_is_listing_disapproval():
    assert tier_for_check_id("form_email_field_weak_validation") == ImpactTier.LISTING_DISAPPROVAL


def test_editorial_quality_is_quality_improvement():
    assert tier_for_check_id("llm_editorial_quality") == ImpactTier.QUALITY_IMPROVEMENT


def test_per_item_product_data_checks_are_listing_disapproval():
    for check_id in ["woocommerce_price_mismatch", "shopify_availability_mismatch", "generic_product_price_missing"]:
        assert tier_for_check_id(check_id) == ImpactTier.LISTING_DISAPPROVAL


def test_unknown_check_id_defaults_to_listing_disapproval():
    assert tier_for_check_id("some_future_check_not_yet_classified") == ImpactTier.LISTING_DISAPPROVAL


def test_ambiguous_check_ids_are_all_listing_disapproval():
    assert AMBIGUOUS_CHECK_IDS
    for check_id in AMBIGUOUS_CHECK_IDS:
        assert tier_for_check_id(check_id) == ImpactTier.LISTING_DISAPPROVAL


def test_citation_present_for_grounded_check_ids():
    citation = citation_for_check_id("business_identity_present")
    assert citation is not None
    assert "answer/6150127" in citation or "misrepresentation" in citation.lower()


def test_citation_none_for_unknown_check_id():
    assert citation_for_check_id("not_a_real_check_id") is None


def test_apply_impact_tiers_does_not_mutate_input():
    original = _finding("llm_prohibited_content")
    result = apply_impact_tiers([original])
    assert original.impact_tier == ImpactTier.LISTING_DISAPPROVAL  # untouched default
    assert result[0].impact_tier == ImpactTier.SUSPENSION_RISK
    assert result[0] is not original


def test_apply_impact_tiers_preserves_other_fields():
    original = _finding("llm_prohibited_content")
    result = apply_impact_tiers([original])[0]
    assert result.check_id == original.check_id
    assert result.title == original.title
    assert result.evidence == original.evidence
