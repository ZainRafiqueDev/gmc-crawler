"""Maps every real check_id (see Finding.check_id across app/checks and
app/llm) to one of the 8 ground-truth categories used in validation_set.json.

Single source of truth for that mapping - both score_validation.py and anyone
filling in the checklist template should treat this file as the definition of
what each category covers, not the prose in the template.
"""
from __future__ import annotations

CATEGORY_CHECK_IDS: dict[str, list[str]] = {
    "required_page_presence": [
        "required_page_present",
    ],
    "external_domain_links": [
        "external_domain_link",
    ],
    "business_identity_consistency": [
        "business_identity_present",
        "business_identity_email_consistency",
        "business_identity_phone_consistency",
        "business_identity_phone_country_mismatch",
    ],
    "broken_links_images": [
        "broken_internal_link",
        "broken_image",
        "https_enforced",
        "https_mixed_content_link",
        "duplicate_nav_footer_block",
    ],
    "product_price_availability_accuracy": [
        "generic_product_price_missing",
        "generic_product_availability_missing",
        "woocommerce_price_mismatch",
        "woocommerce_stock_mismatch",
        "shopify_price_mismatch",
        "shopify_availability_mismatch",
        "purchase_journey_cart_price_mismatch",
        "purchase_journey_cart_price_not_found",
        "purchase_journey_total_lower_than_product_price",
        "purchase_journey_shipping_not_shown",
        "purchase_journey_add_to_cart_failed",
        "purchase_journey_cart_unreachable",
        "purchase_journey_checkout_unreachable",
        "purchase_journey_blocked_ssrf",
    ],
    "product_image_issues": [
        "product_image_broken",
        "product_image_low_resolution",
        "product_image_missing_alt_text",
        "product_image_placeholder_filename",
        "llm_image_product_mismatch",
    ],
    "policy_substance_quality": [
        "llm_editorial_quality",
    ],
    "prohibited_content_risk": [
        "llm_prohibited_content",
        "llm_image_prohibited_content_flag",
    ],
}

CATEGORIES: list[str] = list(CATEGORY_CHECK_IDS.keys())

CHECK_ID_TO_CATEGORY: dict[str, str] = {
    check_id: category
    for category, check_ids in CATEGORY_CHECK_IDS.items()
    for check_id in check_ids
}


def category_for_check_id(check_id: str) -> str | None:
    """None means this check_id isn't mapped to any of the 8 ground-truth
    categories (e.g. llm_image_vision_check, which is diagnostic rather than
    a pass/fail finding) - callers should ignore it, not guess a bucket."""
    return CHECK_ID_TO_CATEGORY.get(check_id)
