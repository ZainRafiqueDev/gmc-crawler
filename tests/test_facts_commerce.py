"""Tests for app.facts.commerce_facts - shipping/returns/payment extractors
(work-order step 4, piece 1). return.window_days, shipping.processing_days,
shipping.delivery_days, and return.refund_terms moved to LLM-assisted
extraction (app.facts.llm_commerce_facts, tests in test_facts_llm_commerce.py)
- the regex functions behind them still live in this module and are tested
there directly, as the reused "fast path" building block.
"""
from __future__ import annotations

from app.facts.commerce_facts import extract_commerce_facts
from app.facts.keys import (
    PAYMENT_METHODS,
    RETURN_COST_RESPONSIBILITY,
    SHIPPING_COST_AMOUNT,
    SHIPPING_COST_CURRENCY,
    SHIPPING_REGION,
)
from app.models import CrawledPage, PageType, SiteMap


def _facts_of(facts, key):
    return [f for f in facts if f.fact == key]


def _site_map_with(page: CrawledPage) -> SiteMap:
    return SiteMap(base_url="https://example.com", pages=[page])


def test_shipping_region_country_list():
    page = CrawledPage(
        url="https://example.com/shipping", page_type=PageType.SHIPPING_POLICY, depth=1,
        text="We currently ship to the United States, Canada, and the United Kingdom.",
    )
    facts = extract_commerce_facts(_site_map_with(page))
    region = _facts_of(facts, SHIPPING_REGION)
    assert len(region) == 1
    assert region[0].value == "CA,GB,US"  # sorted, comma-joined


def test_shipping_region_worldwide_sentinel():
    page = CrawledPage(
        url="https://example.com/shipping", page_type=PageType.SHIPPING_POLICY, depth=1,
        text="We offer worldwide shipping on every order.",
    )
    facts = extract_commerce_facts(_site_map_with(page))
    region = _facts_of(facts, SHIPPING_REGION)
    assert region[0].value == "WORLDWIDE"


def test_shipping_cost_amount_and_currency():
    page = CrawledPage(
        url="https://example.com/shipping", page_type=PageType.SHIPPING_POLICY, depth=1,
        text="Standard shipping costs $7.50 within the continental US.",
    )
    facts = extract_commerce_facts(_site_map_with(page))
    assert _facts_of(facts, SHIPPING_COST_AMOUNT)[0].value == "7.50"
    assert _facts_of(facts, SHIPPING_COST_CURRENCY)[0].value == "USD"


def test_shipping_cost_free_has_amount_zero_no_currency_fact():
    page = CrawledPage(
        url="https://example.com/shipping", page_type=PageType.SHIPPING_POLICY, depth=1,
        text="We offer free shipping on all orders over $50.",
    )
    facts = extract_commerce_facts(_site_map_with(page))
    amounts = _facts_of(facts, SHIPPING_COST_AMOUNT)
    assert amounts[0].value == "0"
    assert _facts_of(facts, SHIPPING_COST_CURRENCY) == []


def test_return_cost_responsibility_classification():
    page = CrawledPage(
        url="https://example.com/returns", page_type=PageType.RETURNS_POLICY, depth=1,
        text="Customer is responsible for return shipping costs.",
    )
    facts = extract_commerce_facts(_site_map_with(page))
    assert _facts_of(facts, RETURN_COST_RESPONSIBILITY)[0].value == "customer_pays"


def test_payment_methods_set():
    page = CrawledPage(
        url="https://example.com", page_type=PageType.HOMEPAGE, depth=0,
        text="We accept Visa, Mastercard, PayPal, and Apple Pay.",
    )
    facts = extract_commerce_facts(_site_map_with(page))
    methods = _facts_of(facts, PAYMENT_METHODS)
    assert methods[0].value == "APPLE_PAY,MASTERCARD,PAYPAL,VISA"


def test_no_matching_pattern_emits_no_fact():
    page = CrawledPage(
        url="https://example.com/shipping", page_type=PageType.SHIPPING_POLICY, depth=1,
        text="Thanks for shopping with us.",
    )
    facts = extract_commerce_facts(_site_map_with(page))
    assert facts == []
