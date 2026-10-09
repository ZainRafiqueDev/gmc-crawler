"""Canonical fact-key constants - see app/facts/canonical_facts.md for the
full, locked dictionary (value type, normalization rule, expected pages,
comparison scope, method(s) per key). Extractors import these constants
rather than hardcoding key strings, so a typo in a key name is a NameError at
import time, not a silent "this fact never matches anything" bug later.

FACT_REGISTRY mirrors canonical_facts.md's tables in code (scope + valid
method tags per key) - used for lightweight sanity-checking in tests, and by
work-order piece 4 to validate that every rule's referenced fact key is a
real, known key (see decisions.md's "Rule-key rename" entry).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FactScope(str, Enum):
    SITE_WIDE = "site_wide"
    PER_RESOURCE = "per_resource"


# --- Business identity ---
BUSINESS_NAME = "business.name"
BUSINESS_LEGAL_ENTITY = "business.legal_entity"
BUSINESS_EMAIL = "business.email"
BUSINESS_PHONE = "business.phone"
BUSINESS_ADDRESS = "business.address"
BUSINESS_ADDRESS_COUNTRY = "business.address_country"

# --- Shipping ---
SHIPPING_REGION = "shipping.region"
SHIPPING_COST_AMOUNT = "shipping.cost.amount"
SHIPPING_COST_CURRENCY = "shipping.cost.currency"
SHIPPING_PROCESSING_DAYS = "shipping.processing_days"
SHIPPING_DELIVERY_DAYS = "shipping.delivery_days"

# --- Returns ---
RETURN_WINDOW_DAYS = "return.window_days"
RETURN_REFUND_TERMS = "return.refund_terms"
RETURN_COST_RESPONSIBILITY = "return.cost_responsibility"

# --- Payment ---
PAYMENT_METHODS = "payment.methods"

# --- Product (JSON-LD + visible HTML + platform API - piece 2, not piece 1) ---
PRODUCT_PRICE_AMOUNT = "product.price.amount"
PRODUCT_PRICE_CURRENCY = "product.price.currency"
PRODUCT_AVAILABILITY = "product.availability"
PRODUCT_BRAND = "product.brand"
PRODUCT_GTIN = "product.gtin"
PRODUCT_MPN = "product.mpn"
PRODUCT_SKU = "product.sku"
PRODUCT_CONDITION = "product.condition"


@dataclass(frozen=True)
class FactKeyInfo:
    scope: FactScope
    methods: tuple[str, ...]


FACT_REGISTRY: dict[str, FactKeyInfo] = {
    BUSINESS_NAME: FactKeyInfo(FactScope.SITE_WIDE, ("copyright_footer_regex", "heading_heuristic")),
    BUSINESS_LEGAL_ENTITY: FactKeyInfo(FactScope.SITE_WIDE, ("copyright_footer_regex", "legal_suffix_regex")),
    BUSINESS_EMAIL: FactKeyInfo(FactScope.SITE_WIDE, ("business_identity_regex",)),
    BUSINESS_PHONE: FactKeyInfo(FactScope.SITE_WIDE, ("business_identity_regex",)),
    BUSINESS_ADDRESS: FactKeyInfo(FactScope.SITE_WIDE, ("business_identity_regex",)),
    BUSINESS_ADDRESS_COUNTRY: FactKeyInfo(FactScope.SITE_WIDE, ("calling_code_cross_reference", "address_text_country_regex")),
    SHIPPING_REGION: FactKeyInfo(FactScope.SITE_WIDE, ("shipping_region_regex",)),
    SHIPPING_COST_AMOUNT: FactKeyInfo(FactScope.SITE_WIDE, ("shipping_cost_regex",)),
    SHIPPING_COST_CURRENCY: FactKeyInfo(FactScope.SITE_WIDE, ("shipping_cost_regex",)),
    SHIPPING_PROCESSING_DAYS: FactKeyInfo(FactScope.SITE_WIDE, ("shipping_time_regex", "llm", "regex_llm_agree")),
    SHIPPING_DELIVERY_DAYS: FactKeyInfo(FactScope.SITE_WIDE, ("shipping_time_regex", "llm", "regex_llm_agree")),
    RETURN_WINDOW_DAYS: FactKeyInfo(FactScope.SITE_WIDE, ("return_window_regex", "llm", "regex_llm_agree")),
    RETURN_REFUND_TERMS: FactKeyInfo(FactScope.SITE_WIDE, ("refund_terms_keyword_classifier", "llm", "regex_llm_agree")),
    RETURN_COST_RESPONSIBILITY: FactKeyInfo(FactScope.SITE_WIDE, ("return_cost_keyword_classifier",)),
    PAYMENT_METHODS: FactKeyInfo(FactScope.SITE_WIDE, ("payment_methods_keyword_regex",)),
    PRODUCT_PRICE_AMOUNT: FactKeyInfo(FactScope.PER_RESOURCE, ("jsonld", "visible_price_regex", "platform_api")),
    PRODUCT_PRICE_CURRENCY: FactKeyInfo(FactScope.PER_RESOURCE, ("jsonld", "visible_price_regex", "platform_api")),
    PRODUCT_AVAILABILITY: FactKeyInfo(FactScope.PER_RESOURCE, ("jsonld", "visible_availability_heuristic", "platform_api")),
    PRODUCT_BRAND: FactKeyInfo(FactScope.PER_RESOURCE, ("jsonld",)),
    PRODUCT_GTIN: FactKeyInfo(FactScope.PER_RESOURCE, ("jsonld",)),
    PRODUCT_MPN: FactKeyInfo(FactScope.PER_RESOURCE, ("jsonld",)),
    PRODUCT_SKU: FactKeyInfo(FactScope.PER_RESOURCE, ("jsonld",)),
    PRODUCT_CONDITION: FactKeyInfo(FactScope.PER_RESOURCE, ("jsonld",)),
}

# Base confidence per extraction method - app/facts/canonical_facts.md's
# "Method -> base confidence" table, in code. A fact's ResourceFact.confidence
# is always this value for its method, never hand-tuned per instance.
METHOD_BASE_CONFIDENCE: dict[str, float] = {
    "platform_api": 0.98,
    "jsonld": 0.95,
    # Two independent signals (regex + LLM) agreeing on the same value -
    # structural evidence, fixed like every other method here. Unlike "llm"
    # below, this is never overridden per-instance.
    "regex_llm_agree": 0.95,
    "business_identity_regex": 0.90,
    # Default/fallback only - app.facts.llm_commerce_facts never actually
    # calls confidence_for_method("llm"); an LLM-sourced commerce fact
    # carries the model's own reported confidence for that specific
    # extraction instead (the one deliberate exception to "confidence is a
    # pure function of method" - semantic extraction reliability genuinely
    # varies per instance, not per method). This entry exists so
    # confidence_for_method("llm") still returns something sane if any other
    # code path ever calls it generically.
    "llm": 0.75,
    "shipping_time_regex": 0.80,
    "return_window_regex": 0.80,
    "shipping_cost_regex": 0.80,
    "shipping_region_regex": 0.80,
    "copyright_footer_regex": 0.75,
    "legal_suffix_regex": 0.75,
    "heading_heuristic": 0.75,
    "refund_terms_keyword_classifier": 0.70,
    "return_cost_keyword_classifier": 0.70,
    "payment_methods_keyword_regex": 0.70,
    "visible_price_regex": 0.70,
    "visible_availability_heuristic": 0.65,
    "calling_code_cross_reference": 0.60,
    "address_text_country_regex": 0.60,
}


def confidence_for_method(method: str) -> float:
    try:
        return METHOD_BASE_CONFIDENCE[method]
    except KeyError:
        raise ValueError(f"unknown extraction method: {method!r} - add it to METHOD_BASE_CONFIDENCE") from None
