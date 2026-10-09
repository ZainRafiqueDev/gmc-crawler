"""Tests for app.facts.normalize - the shared helpers every extractor (and,
in piece 2, JSON-LD/product facts) builds canonical values through."""
from __future__ import annotations

from app.facts.normalize import (
    find_country_mentions,
    is_worldwide_shipping_mention,
    normalize_availability,
    normalize_condition,
    normalize_gtin,
    normalize_identifier,
    parse_day_range,
    parse_money,
)


def test_find_country_mentions_matches_name_and_abbreviation():
    assert find_country_mentions("We ship from the United States and Canada") == {"US", "CA"}
    assert find_country_mentions("Based in the UK") == {"GB"}
    assert find_country_mentions("No country mentioned here") == set()


def test_worldwide_shipping_mention():
    assert is_worldwide_shipping_mention("We offer worldwide shipping") is True
    assert is_worldwide_shipping_mention("We ship to the US only") is False


def test_parse_money_symbol_amount():
    assert parse_money("Flat rate shipping: $9.99") == ("9.99", "USD")
    assert parse_money("Shipping costs £4.50 for UK orders") == ("4.50", "GBP")


def test_parse_money_amount_then_code():
    assert parse_money("Shipping is 12.00 EUR for EU orders") == ("12.00", "EUR")


def test_parse_money_free_shipping_has_no_currency():
    assert parse_money("Enjoy free shipping on all orders") == ("0", None)


def test_parse_money_no_pattern_returns_none_none():
    assert parse_money("We ship fast and reliably") == (None, None)


def test_parse_day_range_same_day():
    assert parse_day_range("Orders ship same day") == 0


def test_parse_day_range_within_hours():
    assert parse_day_range("Orders ship within 12 hours") == 1
    assert parse_day_range("Orders ship within 48 hours") == 2


def test_parse_day_range_upper_bound_of_range():
    assert parse_day_range("Delivery takes 3-5 business days") == 5
    assert parse_day_range("Delivery takes 3 to 5 days") == 5


def test_parse_day_range_single_value():
    assert parse_day_range("Delivery takes 7 business days") == 7


def test_parse_day_range_no_pattern_returns_none():
    assert parse_day_range("Delivery is fast") is None


def test_normalize_availability_schema_org_and_platform_vocab_agree():
    assert normalize_availability("https://schema.org/InStock") == "in_stock"
    assert normalize_availability("http://schema.org/InStock") == "in_stock"
    assert normalize_availability("InStock") == "in_stock"
    assert normalize_availability("instock") == "in_stock"  # WooCommerce stock_status
    assert normalize_availability("OutOfStock") == "out_of_stock"
    assert normalize_availability("outofstock") == "out_of_stock"
    assert normalize_availability("onbackorder") == "preorder"
    assert normalize_availability("PreOrder") == "preorder"


def test_normalize_availability_shopify_bool():
    assert normalize_availability(True) == "in_stock"
    assert normalize_availability(False) == "out_of_stock"


def test_normalize_availability_unknown_is_unspecified_never_guessed():
    assert normalize_availability("SomeWeirdToken") == "unspecified"
    assert normalize_availability(None) == "unspecified"


def test_normalize_gtin_pads_to_14_digits():
    assert normalize_gtin("012345678905") == "00012345678905"  # GTIN-12 -> GTIN-14
    assert normalize_gtin("0123456789012") == "00123456789012"  # GTIN-13 -> GTIN-14
    assert normalize_gtin("00012345678905") == "00012345678905"  # already 14


def test_normalize_gtin_strips_non_digits():
    assert normalize_gtin("012-345-678-905") == normalize_gtin("012345678905")


def test_normalize_gtin_equivalent_forms_compare_equal():
    # The exact false-flag this is meant to prevent: a GTIN-12 from one
    # source and the same product's zero-padded GTIN-14 from another.
    assert normalize_gtin("012345678905") == normalize_gtin("00012345678905")


def test_normalize_gtin_none_or_empty():
    assert normalize_gtin(None) is None
    assert normalize_gtin("") is None


def test_normalize_condition_schema_org_and_bare_tokens():
    assert normalize_condition("https://schema.org/UsedCondition") == "used"
    assert normalize_condition("NewCondition") == "new"
    assert normalize_condition("Refurbished") == "refurbished"
    assert normalize_condition("SomethingElse") == "unspecified"
    assert normalize_condition(None) == "unspecified"


def test_normalize_identifier_strips_formatting_and_uppercases():
    assert normalize_identifier("abc-123") == "ABC123"
    assert normalize_identifier("ABC 123") == "ABC123"
    assert normalize_identifier("abc_123") == "ABC123"
    assert normalize_identifier(None) is None
