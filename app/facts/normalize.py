"""Shared normalization helpers - see app/facts/canonical_facts.md's "Shared
normalization helpers" section for the rationale behind each. Every
extractor (business identity, shipping/returns/payment - piece 1; JSON-LD/
product - piece 2) calls through these rather than reimplementing parsing,
so two extractors never normalize the same kind of raw value two different
ways.
"""
from __future__ import annotations

import math
import re

from app.facts.keys import (
    PRODUCT_GTIN,
    PRODUCT_MPN,
    PRODUCT_PRICE_AMOUNT,
    PRODUCT_SKU,
    RETURN_WINDOW_DAYS,
    SHIPPING_COST_AMOUNT,
    SHIPPING_DELIVERY_DAYS,
    SHIPPING_PROCESSING_DAYS,
)

# --- Country names -> ISO-3166-1 alpha-2 ------------------------------------
# Generalizes app.checks.business_identity._CALLING_CODE_COUNTRIES's existing
# name fragments (reused verbatim, not re-typed) into a name->alpha-2 lookup,
# independent of calling codes - a name fragment maps to exactly one country,
# even where a calling code is itself ambiguous (e.g. "+1" is US or CA).
COUNTRY_NAME_TO_ALPHA2: dict[str, str] = {
    "united states": "US", "usa": "US", "u.s.a": "US",
    "canada": "CA",
    "united kingdom": "GB", "uk": "GB", "england": "GB", "scotland": "GB", "wales": "GB", "britain": "GB",
    "australia": "AU",
    "new zealand": "NZ",
    "india": "IN",
    "germany": "DE",
    "france": "FR",
    "spain": "ES",
    "italy": "IT",
    "netherlands": "NL",
    "ireland": "IE",
    "south africa": "ZA",
    "singapore": "SG",
    "united arab emirates": "AE", "uae": "AE", "dubai": "AE",
    "pakistan": "PK",
}

_WORLDWIDE_RE = re.compile(r"\b(worldwide|globally|international shipping|all countries|ship(s)? anywhere)\b", re.IGNORECASE)


def find_country_mentions(text: str) -> set[str]:
    """Every distinct ISO alpha-2 code whose name fragment appears in `text`
    (case-insensitive substring match) - used by business.address_country and
    shipping.region. Returns an empty set, never a guess, when nothing matches."""
    lowered = text.lower()
    return {alpha2 for fragment, alpha2 in COUNTRY_NAME_TO_ALPHA2.items() if fragment in lowered}


def is_worldwide_shipping_mention(text: str) -> bool:
    return bool(_WORLDWIDE_RE.search(text))


# --- Money: symbol -> ISO-4217, amount parsed separately from currency -----
MONEY_SYMBOL_TO_CURRENCY: dict[str, str] = {
    "$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY", "₹": "INR",
}
_MONEY_CODE_RE = re.compile(r"\b(USD|GBP|EUR|JPY|INR|CAD|AUD)\b", re.IGNORECASE)
_MONEY_SYMBOL_AMOUNT_RE = re.compile(
    r"(?P<symbol>[$£€¥₹])\s?(?P<amount>\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)"
)
_MONEY_AMOUNT_CODE_RE = re.compile(
    r"(?P<amount>\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)\s?(?P<code>USD|GBP|EUR|JPY|INR|CAD|AUD)\b", re.IGNORECASE
)
_FREE_SHIPPING_RE = re.compile(r"\bfree\s+(shipping|delivery)\b", re.IGNORECASE)


def parse_money(text: str) -> tuple[str | None, str | None]:
    """Returns (amount_as_str, iso_4217_currency), either half possibly None.
    "free shipping"/"free delivery" -> ("0", None) - the cost IS known (zero),
    the currency genuinely isn't (never guessed for a cost that was never
    priced in one). No money-like pattern found at all -> (None, None) - no
    fact should be emitted, not a fabricated zero.
    """
    if _FREE_SHIPPING_RE.search(text):
        return "0", None

    m = _MONEY_SYMBOL_AMOUNT_RE.search(text)
    if m:
        amount = m.group("amount").replace(",", "")
        currency = MONEY_SYMBOL_TO_CURRENCY.get(m.group("symbol"))
        return amount, currency

    m = _MONEY_AMOUNT_CODE_RE.search(text)
    if m:
        return m.group("amount").replace(",", ""), m.group("code").upper()

    return None, None


# --- Day ranges -> single int (upper bound) ---------------------------------
_SAME_DAY_RE = re.compile(r"\bsame[- ]day\b", re.IGNORECASE)
_WITHIN_HOURS_RE = re.compile(r"\bwithin\s+(\d+)\s+hours?\b", re.IGNORECASE)
_DAY_RANGE_RE = re.compile(r"\b(\d+)\s*(?:-|to)\s*(\d+)\s*(?:business\s+)?days?\b", re.IGNORECASE)
_SINGLE_DAY_RE = re.compile(r"\b(\d+)\s*(?:business\s+)?days?\b", re.IGNORECASE)


def parse_day_range(text: str) -> int | None:
    """"1-2 business days" -> 2 (upper bound - the number actually committed
    to); "same day" -> 0; "within N hours" -> 1 if N<=24 else ceil(N/24);
    "N days" -> N. None when no day/hour pattern is present at all."""
    if _SAME_DAY_RE.search(text):
        return 0

    m = _WITHIN_HOURS_RE.search(text)
    if m:
        hours = int(m.group(1))
        return 1 if hours <= 24 else math.ceil(hours / 24)

    m = _DAY_RANGE_RE.search(text)
    if m:
        return max(int(m.group(1)), int(m.group(2)))

    m = _SINGLE_DAY_RE.search(text)
    if m:
        return int(m.group(1))

    return None


# --- Availability: one canonical enum, multiple real-world vocabularies ----
_AVAILABILITY_MAP: dict[str, str] = {
    # schema.org (full URL or bare last-path-segment, lowercased)
    "instock": "in_stock",
    "limitedavailability": "in_stock",
    "outofstock": "out_of_stock",
    "soldout": "out_of_stock",
    "discontinued": "out_of_stock",
    "preorder": "preorder",
    "presale": "preorder",
    "backorder": "preorder",
    # WooCommerce REST stock_status (app.checks.woocommerce_products)
    "onbackorder": "preorder",
}


_CONDITION_MAP: dict[str, str] = {
    "newcondition": "new", "new": "new",
    "usedcondition": "used", "used": "used",
    "refurbishedcondition": "refurbished", "refurbished": "refurbished",
}


def normalize_condition(raw: str | None) -> str:
    """Same URL/bare-token handling as normalize_availability - schema.org's
    itemCondition (e.g. "https://schema.org/UsedCondition") or a bare token
    ("Used"), case-insensitive. An unrecognized or absent value is
    "unspecified" - never guessed."""
    if not raw:
        return "unspecified"
    token = raw.rstrip("/").rsplit("/", 1)[-1].lower()
    return _CONDITION_MAP.get(token, "unspecified")


def normalize_availability(raw: str | bool | None) -> str:
    """Maps BOTH schema.org's Offer.availability vocabulary AND the
    WooCommerce/Shopify platform-API vocabularies to the same canonical
    token - this is what lets an availability-mismatch rule compare
    `InStock` (JSON-LD) against `instock` (WooCommerce API) correctly
    instead of false-flagging a cosmetic vocabulary difference. Shopify's
    `available` field is a bool, not a string - handled directly rather than
    stringified first."""
    if isinstance(raw, bool):
        return "in_stock" if raw else "out_of_stock"
    if not raw:
        return "unspecified"

    token = raw.rstrip("/").rsplit("/", 1)[-1].lower()
    return _AVAILABILITY_MAP.get(token, "unspecified")


# --- Identifiers -------------------------------------------------------------
def normalize_gtin(raw: str | None) -> str | None:
    """Strips everything but digits, then left-pads to 14 digits - GS1's own
    equivalence rule (a GTIN-8/12/13 IS the zero-padded prefix of its GTIN-14
    form), so a GTIN-12 from one source and the "same" product's GTIN-14 from
    another compare equal instead of false-flagging. Does not validate the
    GS1 check digit - this only normalizes form."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    return digits.zfill(14) if len(digits) <= 14 else digits


def normalize_text_for_compare(value: str) -> str:
    """Engine-wide default comparison normalization (canonical_facts.md: any
    fact not marked otherwise is compared case-insensitively, whitespace-
    collapsed). Applied at COMPARISON time, not at storage time, so the
    stored value stays human-readable evidence."""
    return " ".join(value.split()).casefold()


# Per-fact-type numeric tolerance for the contradiction engine
# (app.contradiction_engine) - "30 days" vs "30" must compare equal (same
# number, different string formatting), and a cent-level rounding difference
# between two price sources must not read as a contradiction. 0.0 still means
# "exact numeric equality", not "skip the check" - it just rejects float-vs-
# string-formatting noise, not genuine value differences.
from app.facts.keys import (  # noqa: E402 - avoids a circular import at module load time
    PRODUCT_GTIN,
    PRODUCT_MPN,
    PRODUCT_PRICE_AMOUNT,
    PRODUCT_SKU,
    RETURN_WINDOW_DAYS,
    SHIPPING_COST_AMOUNT,
    SHIPPING_DELIVERY_DAYS,
    SHIPPING_PROCESSING_DAYS,
)

NUMERIC_FACT_TOLERANCE: dict[str, float] = {
    RETURN_WINDOW_DAYS: 0.0,
    SHIPPING_PROCESSING_DAYS: 0.0,
    SHIPPING_DELIVERY_DAYS: 0.0,
    SHIPPING_COST_AMOUNT: 0.01,
    PRODUCT_PRICE_AMOUNT: 0.01,
}

# Compared verbatim, never casefolded - a cosmetic-case difference in a GTIN/
# MPN/SKU is not the point; both are already normalized to one canonical
# casing at extraction time (normalize_gtin/normalize_identifier), so a real
# difference here is a real difference.
VERBATIM_COMPARE_FACTS: set[str] = {PRODUCT_GTIN, PRODUCT_MPN, PRODUCT_SKU}


def values_equal(fact_key: str, value_a: str, value_b: str) -> bool:
    """The ONE comparison function the contradiction engine calls - every
    fact key's comparison rule (numeric-with-tolerance, verbatim, or the
    engine-wide case-insensitive default) lives here, not duplicated at the
    call site."""
    if fact_key in NUMERIC_FACT_TOLERANCE:
        try:
            return abs(float(value_a) - float(value_b)) <= NUMERIC_FACT_TOLERANCE[fact_key]
        except ValueError:
            return normalize_text_for_compare(value_a) == normalize_text_for_compare(value_b)
    if fact_key in VERBATIM_COMPARE_FACTS:
        return value_a == value_b
    return normalize_text_for_compare(value_a) == normalize_text_for_compare(value_b)


def normalize_identifier(raw: str | None) -> str | None:
    """MPN/SKU: strip non-alphanumeric characters, uppercase. Neither is
    GS1-standardized (unlike GTIN), so no length/padding rule applies - only
    cosmetic formatting ("ABC-123" vs "abc123" vs "ABC 123") is normalized."""
    if not raw:
        return None
    stripped = re.sub(r"[^a-zA-Z0-9]", "", raw)
    return stripped.upper() or None
