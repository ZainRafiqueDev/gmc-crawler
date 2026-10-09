"""Shipping/returns/payment fact extractors - new patterns (no existing
check to generalize, unlike business identity), but the same Phase-1
regex/keyword-classifier style as app.checks.business_identity: every fact
carries its exact source text as evidence, nothing is inferred without a
textual signal, and a page with no matching pattern simply contributes no
fact row (absence of a row IS "not stated/unknown" - never a guessed
"unspecified" sentinel value).
"""
from __future__ import annotations

import re

from app.facts.keys import (
    PAYMENT_METHODS,
    RETURN_COST_RESPONSIBILITY,
    RETURN_WINDOW_DAYS,
    SHIPPING_COST_AMOUNT,
    SHIPPING_COST_CURRENCY,
    SHIPPING_REGION,
)
from app.facts.normalize import find_country_mentions, is_worldwide_shipping_mention, parse_day_range, parse_money
from app.facts.types import FactRecord, make_fact
from app.models import CrawledPage, PageType, SiteMap

_SHIPPING_REGION_PAGES = {PageType.HOMEPAGE, PageType.SHIPPING_POLICY}
_SHIPPING_COST_PAGES = {PageType.SHIPPING_POLICY, PageType.PRODUCT, PageType.CHECKOUT}
_PROCESSING_DAYS_PAGES = {PageType.SHIPPING_POLICY, PageType.PRODUCT, PageType.FAQ}
_DELIVERY_DAYS_PAGES = {PageType.SHIPPING_POLICY, PageType.PRODUCT, PageType.FAQ, PageType.CHECKOUT}
_RETURN_WINDOW_PAGES = {PageType.RETURNS_POLICY, PageType.FAQ, PageType.PRODUCT, PageType.CHECKOUT}
_REFUND_TERMS_PAGES = {PageType.RETURNS_POLICY, PageType.FAQ}
_RETURN_COST_PAGES = {PageType.RETURNS_POLICY, PageType.FAQ}
_PAYMENT_PAGES = {PageType.HOMEPAGE, PageType.CHECKOUT, PageType.FAQ}

_PROCESSING_KEYWORDS = ("processing time", "handling time", "ships within", "order processing", "processed within", "dispatch")
_DELIVERY_KEYWORDS = ("delivery time", "arrives in", "transit time", "estimated delivery", "delivery within", "arrive within", "shipping time")
_FINAL_SALE_RE = re.compile(r"\b(final sale|no returns)\b", re.IGNORECASE)

_REFUND_TERMS_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bstore credit\b", re.IGNORECASE), "store_credit_only"),
    (re.compile(r"\bexchanges?\s+only\b", re.IGNORECASE), "exchange_only"),
    (re.compile(r"\b(restocking fee|partial refund)\b", re.IGNORECASE), "partial_refund"),
    (re.compile(r"\b(full refund|money\s*back|100%\s*refund)\b", re.IGNORECASE), "full_refund"),
)

_RETURN_COST_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bfree returns? on orders? (over|above)\b", re.IGNORECASE), "free_above_threshold"),
    (re.compile(r"\b(free returns?|we (cover|pay for) return shipping|prepaid return label)\b", re.IGNORECASE), "merchant_pays"),
    (re.compile(r"\b(customer (is responsible for|pays) return shipping|you (are|will be) responsible for return shipping)\b", re.IGNORECASE), "customer_pays"),
)

# Known false-positive risk, same as business_identity.py's own address
# regex: "discover" is a common English verb, so a bare, unqualified
# "Discover" mention (not "Discover Card"/"Discover Network") can false-
# match. Accepted Phase-1 heuristic risk, documented rather than silently
# ignored - every emitted fact carries its exact matched source text, so a
# false match is visible and dismissible, not hidden.
_PAYMENT_KEYWORD_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bvisa\b", re.IGNORECASE), "VISA"),
    (re.compile(r"\bmaster\s?card\b", re.IGNORECASE), "MASTERCARD"),
    (re.compile(r"\b(american express|amex)\b", re.IGNORECASE), "AMEX"),
    (re.compile(r"\bdiscover\s*(card|network)?\b", re.IGNORECASE), "DISCOVER"),
    (re.compile(r"\bpaypal\b", re.IGNORECASE), "PAYPAL"),
    (re.compile(r"\bapple\s?pay\b", re.IGNORECASE), "APPLE_PAY"),
    (re.compile(r"\bgoogle\s?pay\b", re.IGNORECASE), "GOOGLE_PAY"),
    (re.compile(r"\bshop\s?pay\b", re.IGNORECASE), "SHOP_PAY"),
    (re.compile(r"\bklarna\b", re.IGNORECASE), "KLARNA"),
    (re.compile(r"\bafterpay\b", re.IGNORECASE), "AFTERPAY"),
)


_SENTENCE_SPLIT_RE = re.compile(r"(?<!\d)[.\n](?!\d)")  # never split a decimal amount like "$7.50"


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


def _day_fact_from_keyword_sentences(page: CrawledPage, fact_key: str, keywords: tuple[str, ...], method: str) -> FactRecord | None:
    for sentence in _sentences(page.text or ""):
        lowered = sentence.lower()
        if not any(kw in lowered for kw in keywords):
            continue
        days = parse_day_range(sentence)
        if days is not None:
            return make_fact(page.url, fact_key, str(days), page.url, sentence, method)
    return None


def _shipping_region_fact(page: CrawledPage) -> FactRecord | None:
    text = page.text or ""
    if is_worldwide_shipping_mention(text):
        return make_fact(page.url, SHIPPING_REGION, "WORLDWIDE", page.url, text[:200], "shipping_region_regex")
    countries = find_country_mentions(text)
    if countries:
        return make_fact(page.url, SHIPPING_REGION, ",".join(sorted(countries)), page.url, text[:200], "shipping_region_regex")
    return None


def _shipping_cost_facts(page: CrawledPage) -> list[FactRecord]:
    facts: list[FactRecord] = []
    for sentence in _sentences(page.text or ""):
        if "ship" not in sentence.lower() and "delivery" not in sentence.lower():
            continue
        amount, currency = parse_money(sentence)
        if amount is None:
            continue
        facts.append(make_fact(page.url, SHIPPING_COST_AMOUNT, amount, page.url, sentence, "shipping_cost_regex"))
        if currency is not None:
            facts.append(make_fact(page.url, SHIPPING_COST_CURRENCY, currency, page.url, sentence, "shipping_cost_regex"))
        break  # one cost statement per page is enough signal; avoid noisy repeats of the same line
    return facts


def _return_window_fact(page: CrawledPage) -> FactRecord | None:
    """The general return-window statement always wins over a "final sale"
    exception/carve-out clause - found live on modcloth.com: a page stated
    both "Final sale items are not returnable" (an exception for specific
    items, not the store's general policy) AND "the 30 day return window"
    (the real, general policy) - checking final-sale first and returning
    immediately meant whichever sentence happened to come first in the page
    decided the fact, fabricating a "0-day" conflict against the real 30-day
    policy stated moments later. A real day-count is searched for FIRST,
    across the whole page; the final-sale fallback ("no window at all") is
    only used when no explicit day-count exists anywhere on the page - the
    genuine "this store doesn't accept returns" case it's meant for.
    """
    text = page.text or ""
    sentences = _sentences(text)

    for sentence in sentences:
        if "return" not in sentence.lower():
            continue
        days = parse_day_range(sentence)
        if days is not None:
            return make_fact(page.url, RETURN_WINDOW_DAYS, str(days), page.url, sentence, "return_window_regex")

    for sentence in sentences:
        if _FINAL_SALE_RE.search(sentence):
            return make_fact(page.url, RETURN_WINDOW_DAYS, "0", page.url, sentence, "return_window_regex")

    return None


def _classify_fact(page: CrawledPage, fact_key: str, rules: tuple[tuple[re.Pattern, str], ...], method: str) -> FactRecord | None:
    text = page.text or ""
    for pattern, value in rules:
        m = pattern.search(text)
        if m:
            return make_fact(page.url, fact_key, value, page.url, m.group(0), method)
    return None


def _payment_methods_fact(page: CrawledPage) -> FactRecord | None:
    text = page.text or ""
    matched = {value for pattern, value in _PAYMENT_KEYWORD_RULES if pattern.search(text)}
    if not matched:
        return None
    return make_fact(page.url, PAYMENT_METHODS, ",".join(sorted(matched)), page.url, text[:200], "payment_methods_keyword_regex")


def extract_commerce_facts(site_map: SiteMap) -> list[FactRecord]:
    """shipping.region, shipping.cost.*, return.cost_responsibility, and
    payment.methods only - purely deterministic, unchanged. The four "free-
    prose" facts this function used to also emit here (return.window_days,
    shipping.processing_days, shipping.delivery_days, return.refund_terms)
    moved to app.facts.llm_commerce_facts: real policy prose varies too much
    for keyword-proximity regex alone to tell what a number refers to
    (confirmed live, twice, on modcloth.com) - those four now go through
    LLM-assisted extraction, with this module's own regex functions reused
    there unchanged as the fast-path cross-check, never duplicated.
    """
    facts: list[FactRecord] = []

    for page in site_map.pages:
        if not page.reachable or not page.text:
            continue

        if page.page_type in _SHIPPING_REGION_PAGES:
            fact = _shipping_region_fact(page)
            if fact is not None:
                facts.append(fact)

        if page.page_type in _SHIPPING_COST_PAGES:
            facts.extend(_shipping_cost_facts(page))

        if page.page_type in _RETURN_COST_PAGES:
            fact = _classify_fact(page, RETURN_COST_RESPONSIBILITY, _RETURN_COST_RULES, "return_cost_keyword_classifier")
            if fact is not None:
                facts.append(fact)

        if page.page_type in _PAYMENT_PAGES:
            fact = _payment_methods_fact(page)
            if fact is not None:
                facts.append(fact)

    return facts
