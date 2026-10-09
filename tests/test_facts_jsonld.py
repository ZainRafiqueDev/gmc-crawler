"""Tests for app.facts.jsonld_facts - JSON-LD product fact extraction (work-
order step 4, piece 2). Each test targets one documented gotcha from
app/facts/canonical_facts.md's "JSON-LD parsing decisions" section.
"""
from __future__ import annotations

import json

from app.facts.jsonld_facts import extract_product_jsonld_facts
from app.facts.keys import (
    PRODUCT_AVAILABILITY,
    PRODUCT_BRAND,
    PRODUCT_CONDITION,
    PRODUCT_GTIN,
    PRODUCT_MPN,
    PRODUCT_PRICE_AMOUNT,
    PRODUCT_PRICE_CURRENCY,
    PRODUCT_SKU,
)
from app.models import CrawledPage, PageType


def _facts_of(facts, key):
    return [f for f in facts if f.fact == key]


def _product_page(html: str, url: str = "https://example.com/product/widget") -> CrawledPage:
    return CrawledPage(url=url, page_type=PageType.PRODUCT, depth=1, html=html, status=200)


def _ldjson_script(data: object) -> str:
    return f'<html><body><script type="application/ld+json">{json.dumps(data)}</script></body></html>'


def test_simple_product_offer_emits_canonical_facts_with_jsonld_method():
    html = _ldjson_script({
        "@type": "Product", "name": "Widget", "brand": "Acme",
        "sku": "ABC-123", "gtin13": "0123456789012",
        "offers": {"@type": "Offer", "price": "19.99", "priceCurrency": "USD", "availability": "https://schema.org/InStock"},
    })
    facts = extract_product_jsonld_facts(_product_page(html))

    assert _facts_of(facts, PRODUCT_PRICE_AMOUNT)[0].value == "19.99"
    assert _facts_of(facts, PRODUCT_PRICE_AMOUNT)[0].method == "jsonld"
    assert _facts_of(facts, PRODUCT_PRICE_CURRENCY)[0].value == "USD"
    assert _facts_of(facts, PRODUCT_AVAILABILITY)[0].value == "in_stock"
    assert _facts_of(facts, PRODUCT_BRAND)[0].value == "Acme"
    assert _facts_of(facts, PRODUCT_SKU)[0].value == "ABC123"
    assert _facts_of(facts, PRODUCT_GTIN)[0].value == "00123456789012"  # GTIN-13 -> GTIN-14


def test_product_nested_inside_at_graph():
    html = _ldjson_script({
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebPage", "name": "Widget product page"},
            {"@type": "Product", "name": "Widget", "offers": {"@type": "Offer", "price": "9.99", "priceCurrency": "USD"}},
        ],
    })
    facts = extract_product_jsonld_facts(_product_page(html))
    assert _facts_of(facts, PRODUCT_PRICE_AMOUNT)[0].value == "9.99"


def test_multiple_ldjson_blocks_both_parsed():
    html = (
        '<html><body>'
        '<script type="application/ld+json">{"@type": "Organization", "name": "Acme Corp"}</script>'
        '<script type="application/ld+json">{"@type": "Product", "name": "Widget", '
        '"offers": {"@type": "Offer", "price": "5.00", "priceCurrency": "USD"}}</script>'
        '</body></html>'
    )
    facts = extract_product_jsonld_facts(_product_page(html))
    assert _facts_of(facts, PRODUCT_PRICE_AMOUNT)[0].value == "5.00"


def test_price_as_json_number_not_string():
    html = _ldjson_script({"@type": "Product", "offers": {"@type": "Offer", "price": 19.99, "priceCurrency": "USD"}})
    facts = extract_product_jsonld_facts(_product_page(html))
    assert _facts_of(facts, PRODUCT_PRICE_AMOUNT)[0].value == "19.99"


def test_price_with_thousands_separator():
    html = _ldjson_script({"@type": "Product", "offers": {"@type": "Offer", "price": "1,299.00", "priceCurrency": "USD"}})
    facts = extract_product_jsonld_facts(_product_page(html))
    assert _facts_of(facts, PRODUCT_PRICE_AMOUNT)[0].value == "1299.00"


def test_aggregate_offer_emits_low_price():
    html = _ldjson_script({
        "@type": "Product",
        "offers": {"@type": "AggregateOffer", "lowPrice": "10.00", "highPrice": "25.00", "priceCurrency": "USD"},
    })
    facts = extract_product_jsonld_facts(_product_page(html))
    assert _facts_of(facts, PRODUCT_PRICE_AMOUNT)[0].value == "10.00"


def test_aggregate_offer_with_nested_variant_offers_keeps_both():
    html = _ldjson_script({
        "@type": "Product",
        "offers": {
            "@type": "AggregateOffer", "lowPrice": "10.00", "priceCurrency": "USD",
            "offers": [
                {"@type": "Offer", "sku": "RED", "price": "10.00", "priceCurrency": "USD"},
                {"@type": "Offer", "sku": "BLUE", "price": "12.00", "priceCurrency": "USD"},
            ],
        },
    })
    facts = extract_product_jsonld_facts(_product_page(html))
    prices = {f.value for f in _facts_of(facts, PRODUCT_PRICE_AMOUNT)}
    assert prices == {"10.00", "12.00"}  # aggregate's own 10.00 + both real variants
    skus = {f.value for f in _facts_of(facts, PRODUCT_SKU)}
    assert skus == {"RED", "BLUE"}


def test_offers_array_variants_not_collapsed():
    html = _ldjson_script({
        "@type": "Product",
        "offers": [
            {"@type": "Offer", "sku": "SM", "price": "15.00", "priceCurrency": "USD", "availability": "InStock"},
            {"@type": "Offer", "sku": "LG", "price": "17.00", "priceCurrency": "USD", "availability": "OutOfStock"},
        ],
    })
    facts = extract_product_jsonld_facts(_product_page(html))

    assert {f.value for f in _facts_of(facts, PRODUCT_SKU)} == {"SM", "LG"}
    assert {f.value for f in _facts_of(facts, PRODUCT_PRICE_AMOUNT)} == {"15.00", "17.00"}
    assert {f.value for f in _facts_of(facts, PRODUCT_AVAILABILITY)} == {"in_stock", "out_of_stock"}
    # Evidence distinguishes which variant each price belongs to.
    sm_price = next(f for f in _facts_of(facts, PRODUCT_PRICE_AMOUNT) if f.value == "15.00")
    assert "SM" in sm_price.source_text


def test_availability_full_url_bare_and_https_all_normalize():
    for raw in ("https://schema.org/InStock", "http://schema.org/InStock", "InStock", "instock"):
        html = _ldjson_script({"@type": "Product", "offers": {"@type": "Offer", "price": "1.00", "availability": raw}})
        facts = extract_product_jsonld_facts(_product_page(html))
        assert _facts_of(facts, PRODUCT_AVAILABILITY)[0].value == "in_stock", raw


def test_gtin_variants_all_map_to_canonical_gtin14():
    for field, value, expected in [
        ("gtin8", "12345670", "00000012345670"),
        ("gtin12", "012345678905", "00012345678905"),
        ("gtin13", "0123456789012", "00123456789012"),
        ("gtin14", "00123456789012", "00123456789012"),
        ("gtin", "012345678905", "00012345678905"),
    ]:
        html = _ldjson_script({"@type": "Product", field: value, "offers": {"@type": "Offer", "price": "1.00"}})
        facts = extract_product_jsonld_facts(_product_page(html))
        assert _facts_of(facts, PRODUCT_GTIN)[0].value == expected, field


def test_item_condition_normalized():
    html = _ldjson_script({"@type": "Product", "itemCondition": "https://schema.org/RefurbishedCondition", "offers": {"@type": "Offer", "price": "1.00"}})
    facts = extract_product_jsonld_facts(_product_page(html))
    assert _facts_of(facts, PRODUCT_CONDITION)[0].value == "refurbished"


def test_malformed_block_is_skipped_not_fatal():
    html = (
        '<html><body>'
        '<script type="application/ld+json">{not valid json at all</script>'
        '<script type="application/ld+json">{"@type": "Product", "offers": {"@type": "Offer", "price": "3.00", "priceCurrency": "USD"}}</script>'
        '</body></html>'
    )
    facts = extract_product_jsonld_facts(_product_page(html))  # must not raise
    assert _facts_of(facts, PRODUCT_PRICE_AMOUNT)[0].value == "3.00"


def test_empty_or_null_field_emits_no_fact():
    html = _ldjson_script({"@type": "Product", "sku": "", "mpn": None, "offers": {"@type": "Offer", "price": None, "priceCurrency": "USD"}})
    facts = extract_product_jsonld_facts(_product_page(html))
    assert facts == []  # no price (null), no currency fact without a price, no sku/mpn


def test_non_product_page_returns_no_facts():
    page = CrawledPage(url="https://example.com/about", page_type=PageType.CONTACT_ABOUT, depth=1, html=_ldjson_script({"@type": "Product", "offers": {"price": "1.00"}}), status=200)
    assert extract_product_jsonld_facts(page) == []


def test_unreachable_product_page_returns_no_facts():
    page = CrawledPage(url="https://example.com/product/x", page_type=PageType.PRODUCT, depth=1, html=_ldjson_script({"@type": "Product"}), reachable=False)
    assert extract_product_jsonld_facts(page) == []


def test_no_html_returns_no_facts():
    page = CrawledPage(url="https://example.com/product/x", page_type=PageType.PRODUCT, depth=1, html=None, status=200)
    assert extract_product_jsonld_facts(page) == []


def test_product_group_has_variant_shape_yields_per_variant_facts():
    """Real shape confirmed live on gymshark.com: a top-level @type:
    "ProductGroup" (not "Product") wraps its real variants under
    hasVariant[], each variant its own @type: "Product" carrying its own
    sku/mpn/gtin/offers. Before this fix, extract_product_jsonld_facts
    returned zero facts for this entire shape - the flattener only ever
    walked @graph and plain arrays, never hasVariant.
    """
    html = _ldjson_script({
        "@context": "https://schema.org",
        "@type": "ProductGroup",
        "name": "Shape High Support Push Up Bra",
        "brand": "Gymshark",
        "productGroupID": "B6C5U-UFHB",
        "variesBy": ["size"],
        "hasVariant": [
            {
                "@type": "Product",
                "name": "Shape High Support Push Up Bra - XXS",
                "sku": "B6C5U-UFHB-XXS",
                "mpn": "B6C5U-UFHB-XXS",
                "gtin": "5063699518596",
                "offers": {"@type": "Offer", "price": "32.00", "priceCurrency": "USD", "availability": "https://schema.org/InStock"},
            },
            {
                "@type": "Product",
                "name": "Shape High Support Push Up Bra - S",
                "sku": "B6C5U-UFHB-S",
                "mpn": "B6C5U-UFHB-S",
                "gtin": "5063699518602",
                "offers": {"@type": "Offer", "price": "32.00", "priceCurrency": "USD", "availability": "https://schema.org/OutOfStock"},
            },
        ],
    })
    facts = extract_product_jsonld_facts(_product_page(html))

    skus = {f.value for f in _facts_of(facts, PRODUCT_SKU)}
    assert skus == {"B6C5UUFHBXXS", "B6C5UUFHBS"}  # both variants present - never collapsed

    prices = _facts_of(facts, PRODUCT_PRICE_AMOUNT)
    assert len(prices) == 2
    assert all(p.value == "32.00" and p.method == "jsonld" for p in prices)

    availabilities = {f.value for f in _facts_of(facts, PRODUCT_AVAILABILITY)}
    assert availabilities == {"in_stock", "out_of_stock"}

    gtins = {f.value for f in _facts_of(facts, PRODUCT_GTIN)}
    assert gtins == {"05063699518596", "05063699518602"}  # 13-digit GTIN-13 -> zero-padded GTIN-14

    # Each variant's facts carry a DISTINCT variant_key (gtin preferred over
    # sku - see test_facts_jsonld.py's dedicated variant_key tests for why) -
    # a later rule evaluation can key per-variant comparisons by
    # (resource, variant_key), never accidentally comparing variant A's
    # price against variant B's.
    variant_keys = {f.variant_key for f in prices}
    assert variant_keys == {"05063699518596", "05063699518602"}  # gtin-derived, distinct per variant


# --- variant_key derivation (regression round: real gymshark data has one
# SHARED sku across all size variants, with gtin/mpn as what actually
# varies) ---------------------------------------------------------------

def _variant_group(variants: list[dict]) -> str:
    return _ldjson_script({
        "@context": "https://schema.org", "@type": "ProductGroup", "name": "Test Product", "hasVariant": variants,
    })


def test_shared_sku_distinct_gtin_mpn_still_gets_distinct_variant_keys():
    """The exact real gymshark.com shape: every variant repeats the SAME
    sku (it's the shared parent identifier), but each has its own distinct
    gtin and mpn - variant_key must come from gtin, not sku, or every
    variant's facts collapse into one comparison bucket."""
    html = _variant_group([
        {"@type": "Product", "sku": "B6C5U-UFHB", "mpn": "B6C5U-UFHB-XXS", "gtin": "5063699518596", "offers": {"@type": "Offer", "price": "60.00", "priceCurrency": "USD", "availability": "https://schema.org/OutOfStock"}},
        {"@type": "Product", "sku": "B6C5U-UFHB", "mpn": "B6C5U-UFHB-XS", "gtin": "5063699518404", "offers": {"@type": "Offer", "price": "60.00", "priceCurrency": "USD", "availability": "https://schema.org/InStock"}},
        {"@type": "Product", "sku": "B6C5U-UFHB", "mpn": "B6C5U-UFHB-S", "gtin": "5063699517797", "offers": {"@type": "Offer", "price": "60.00", "priceCurrency": "USD", "availability": "https://schema.org/InStock"}},
    ])
    facts = extract_product_jsonld_facts(_product_page(html))

    prices = _facts_of(facts, PRODUCT_PRICE_AMOUNT)
    assert len(prices) == 3  # never collapsed into one, despite the shared sku
    variant_keys = {f.variant_key for f in prices}
    assert len(variant_keys) == 3  # three distinct keys, one per real variant

    skus = {f.value for f in _facts_of(facts, PRODUCT_SKU)}
    assert skus == {"B6C5UUFHB"}  # the (correctly) shared sku value itself is still reported once per variant...
    sku_variant_keys = {f.variant_key for f in _facts_of(facts, PRODUCT_SKU)}
    assert len(sku_variant_keys) == 3  # ...but each copy is still tagged with its own distinct variant_key


def test_genuinely_identical_variants_still_group_together():
    """Two entries sharing the SAME real identifier (gtin here) are, in
    fact, the same variant - they must still share one variant_key, not be
    artificially split apart."""
    html = _variant_group([
        {"@type": "Product", "sku": "ABC", "gtin": "12345678901234", "offers": {"@type": "Offer", "price": "10.00"}},
        {"@type": "Product", "sku": "ABC", "gtin": "12345678901234", "offers": {"@type": "Offer", "price": "10.00"}},
    ])
    facts = extract_product_jsonld_facts(_product_page(html))
    variant_keys = [f.variant_key for f in _facts_of(facts, PRODUCT_PRICE_AMOUNT)]
    assert len(variant_keys) == 2
    assert variant_keys[0] == variant_keys[1] == "12345678901234"  # already 14 digits - zfill is a no-op


def test_sku_only_product_still_gets_stable_distinct_variant_keys():
    """No gtin, no mpn anywhere - sku is the only identifier available, and
    it DOES vary per variant here, so it must still be used (never
    collapsed to the positional fallback when a real identifier exists)."""
    html = _variant_group([
        {"@type": "Product", "sku": "RED-SHIRT-M", "offers": {"@type": "Offer", "price": "20.00"}},
        {"@type": "Product", "sku": "RED-SHIRT-L", "offers": {"@type": "Offer", "price": "20.00"}},
    ])
    facts = extract_product_jsonld_facts(_product_page(html))
    variant_keys = {f.variant_key for f in _facts_of(facts, PRODUCT_PRICE_AMOUNT)}
    assert variant_keys == {"REDSHIRTM", "REDSHIRTL"}


def test_no_identifier_at_all_falls_back_to_stable_positional_keys_not_all_collapsed():
    """Last resort only: neither gtin, mpn, nor sku present anywhere - still
    must not collapse every variant into one key."""
    html = _variant_group([
        {"@type": "Product", "offers": {"@type": "Offer", "price": "5.00"}},
        {"@type": "Product", "offers": {"@type": "Offer", "price": "7.00"}},
    ])
    facts = extract_product_jsonld_facts(_product_page(html))
    variant_keys = {f.variant_key for f in _facts_of(facts, PRODUCT_PRICE_AMOUNT)}
    assert len(variant_keys) == 2
