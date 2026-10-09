"""LLM-assisted extraction for the "free-prose" commerce facts
(return.window_days, shipping.processing_days, shipping.delivery_days,
return.refund_terms) - the semantic side of the deterministic/LLM line.

Found live, twice, against real stores (modcloth.com): regex keyword-
proximity cannot tell what a number refers to ("processed within 2 business
days" is refund-processing speed, not the return window) and cannot follow a
window stated without the literal word "return" nearby ("Requests must be
opened within 30 days of delivery" on a returns page IS a return-window
statement). Regex stays as a fast, free cross-check - app.facts.commerce_facts's
existing extractors are reused unchanged, never duplicated - but the LLM is
now the thing that decides whether a candidate number is semantically the
right fact, same as every other check in this project that needs judgment
rather than pattern-matching.

Every other commerce fact (shipping.region, shipping.cost.*, payment.methods,
return.cost_responsibility) and every business-identity/product/JSON-LD fact
stays purely deterministic - untouched by this module.

Reconciliation policy:
- LLM says the fact isn't stated at all (null) -> no fact emitted, regardless
  of what regex found. Trusting the LLM's "nothing here" over a regex guess
  is the whole point of this round.
- LLM found a value AND regex's own candidate agrees -> one fact, method
  "regex_llm_agree", a fixed high confidence (two independent signals
  agreeing is strong, structural evidence).
- LLM found a value and regex disagreed or found nothing -> the LLM's value,
  with the LLM's OWN reported confidence (not a fixed per-method constant -
  this is the one deliberate exception to "confidence is a pure function of
  method," since the whole premise is that a semantic extraction's
  reliability varies per instance, not per method).
- The LLM's cited source_sentence is verified against the real page text
  (app.llm.checks.verify_evidence_quote, reused) before any of the above -
  a claimed sentence that doesn't actually appear in the page void the
  extraction entirely, same anti-hallucination discipline as every other
  LLM-graded check in this project.
- No LLM configured (Settings.llm_configured is False) -> falls back to pure
  regex, byte-for-byte the same behavior as before this round existed.
"""
from __future__ import annotations

import asyncio
import logging

from app.config import Settings
from app.facts.commerce_facts import (
    _classify_fact,
    _day_fact_from_keyword_sentences,
    _DELIVERY_DAYS_PAGES,
    _DELIVERY_KEYWORDS,
    _PROCESSING_DAYS_PAGES,
    _PROCESSING_KEYWORDS,
    _REFUND_TERMS_PAGES,
    _REFUND_TERMS_RULES,
    _RETURN_WINDOW_PAGES,
    _return_window_fact,
)
from app.facts.keys import RETURN_REFUND_TERMS, RETURN_WINDOW_DAYS, SHIPPING_DELIVERY_DAYS, SHIPPING_PROCESSING_DAYS
from app.facts.normalize import values_equal
from app.facts.types import FactRecord, confidence_for_method
from app.llm.cache import LLMCache
from app.llm.checks import verify_evidence_quote
from app.llm.client import LLMClient
from app.llm.factory import get_llm_client
from app.models import CrawledPage, SiteMap

logger = logging.getLogger("gmc_audit.facts.llm_commerce_facts")

_LLM_CONCURRENCY = 3
_PAGE_TEXT_LIMIT = 6000

_LLM_COMMERCE_FACT_PAGES = _RETURN_WINDOW_PAGES | _PROCESSING_DAYS_PAGES | _DELIVERY_DAYS_PAGES | _REFUND_TERMS_PAGES

_REFUND_TERMS_ENUM = ["full_refund", "store_credit_only", "exchange_only", "partial_refund", None]

_SYSTEM_PROMPT = (
    "You are a compliance auditor extracting specific merchant policy facts from a webpage's text. "
    "Every source_sentence you return MUST be copied verbatim from the supplied page text - never invent, "
    "paraphrase, or reconstruct a quote from memory. If a fact genuinely isn't stated anywhere on the page, "
    "return null for it and an empty source_sentence rather than guessing.\n\n"
    "These facts are commonly confused in real policy prose - distinguish them carefully:\n"
    "- return_window_days is ONLY the general time a customer has to INITIATE a return or exchange. It is NEVER "
    "refund-processing time (how many days it takes for a refund to post after a return is received) and NEVER "
    "shipping/delivery transit time. A sentence like 'your refund will be processed within 2 business days' "
    "describes processing speed, not the return window - never use it for return_window_days.\n"
    "- A 'final sale' or 'non-returnable' exception for SPECIFIC items is not the general return window. Only "
    "return 0 for return_window_days if the ENTIRE page states no returns are accepted at all, with no general "
    "window stated anywhere else. If a general window (e.g. '30 days') is stated anywhere on the page, use that "
    "number even if a final-sale exception for some items is also mentioned.\n"
    "- A real statement of the return window does not need to contain the literal word 'return' in the same "
    "sentence - read for meaning, not keyword proximity (e.g. 'Requests must be opened within 30 days of the "
    "order's delivery date' on a page about returns IS a return-window statement).\n"
    "- shipping_processing_days is how long before an order SHIPS (handling/dispatch time, before the carrier "
    "has it). shipping_delivery_days is transit time AFTER shipping (how long delivery itself takes). These are "
    "different numbers when a page states both."
)

_COMMERCE_FACTS_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "return_window_days": {"type": ["integer", "null"], "description": "The general return/exchange window in days. 0 only if no returns are accepted at all, anywhere on the page. null if not stated."},
        "return_window_source_sentence": {"type": "string", "description": "Verbatim sentence return_window_days came from. Empty string if null."},
        "return_window_confidence": {"type": "number", "minimum": 0, "maximum": 1, "description": "Confidence this is specifically the general return window - not processing time, not an item-specific exception."},

        "shipping_processing_days": {"type": ["integer", "null"], "description": "Order handling/dispatch time in days, before the carrier takes it. null if not stated."},
        "shipping_processing_source_sentence": {"type": "string", "description": "Verbatim sentence. Empty string if null."},
        "shipping_processing_confidence": {"type": "number", "minimum": 0, "maximum": 1},

        "shipping_delivery_days": {"type": ["integer", "null"], "description": "Transit/delivery time in days, after the order ships. null if not stated."},
        "shipping_delivery_source_sentence": {"type": "string", "description": "Verbatim sentence. Empty string if null."},
        "shipping_delivery_confidence": {"type": "number", "minimum": 0, "maximum": 1},

        "refund_terms": {"type": ["string", "null"], "enum": _REFUND_TERMS_ENUM, "description": "full_refund, store_credit_only, exchange_only, partial_refund, or null if not stated."},
        "refund_terms_source_sentence": {"type": "string", "description": "Verbatim sentence. Empty string if null."},
        "refund_terms_confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "return_window_days", "return_window_source_sentence", "return_window_confidence",
        "shipping_processing_days", "shipping_processing_source_sentence", "shipping_processing_confidence",
        "shipping_delivery_days", "shipping_delivery_source_sentence", "shipping_delivery_confidence",
        "refund_terms", "refund_terms_source_sentence", "refund_terms_confidence",
    ],
}


async def _call_llm_for_page(client: LLMClient, page: CrawledPage) -> dict | None:
    page_text = page.main_content_text or page.text or ""
    user = f"Page URL: {page.url}\nPage text (may be truncated):\n{page_text[:_PAGE_TEXT_LIMIT]}\n\nExtract the facts. Call the tool with your findings."
    return await client.call_tool(_SYSTEM_PROMPT, user, "submit_commerce_facts", _COMMERCE_FACTS_TOOL_SCHEMA)


def _reconcile_one(
    fact_key: str, page: CrawledPage, regex_fact: FactRecord | None,
    llm_value: object, llm_sentence: str, llm_confidence: float,
) -> FactRecord | None:
    if llm_value is None:
        return None  # LLM says this fact isn't stated here - trust that over any regex guess

    page_text = page.main_content_text or page.text or ""
    if not verify_evidence_quote(llm_sentence, page_text):
        logger.warning("LLM commerce-fact extraction for %s on %s cited a sentence not found in the page text - discarding", fact_key, page.url)
        return None

    llm_value_str = str(llm_value)
    if regex_fact is not None and values_equal(fact_key, regex_fact.value, llm_value_str):
        method = "regex_llm_agree"
        confidence = confidence_for_method(method)
    else:
        method = "llm"
        confidence = max(0.0, min(1.0, float(llm_confidence)))

    return FactRecord(
        resource_url=page.url, fact=fact_key, value=llm_value_str,
        source_url=page.url, source_text=llm_sentence, method=method, confidence=confidence,
    )


def _reconcile_page(page: CrawledPage, result: dict | None) -> list[FactRecord]:
    if result is None:
        return []

    facts: list[FactRecord] = []
    if page.page_type in _RETURN_WINDOW_PAGES:
        regex_fact = _return_window_fact(page)
        fact = _reconcile_one(
            RETURN_WINDOW_DAYS, page, regex_fact,
            result.get("return_window_days"), result.get("return_window_source_sentence", ""), result.get("return_window_confidence", 0.0),
        )
        if fact is not None:
            facts.append(fact)

    if page.page_type in _PROCESSING_DAYS_PAGES:
        regex_fact = _day_fact_from_keyword_sentences(page, SHIPPING_PROCESSING_DAYS, _PROCESSING_KEYWORDS, "shipping_time_regex")
        fact = _reconcile_one(
            SHIPPING_PROCESSING_DAYS, page, regex_fact,
            result.get("shipping_processing_days"), result.get("shipping_processing_source_sentence", ""), result.get("shipping_processing_confidence", 0.0),
        )
        if fact is not None:
            facts.append(fact)

    if page.page_type in _DELIVERY_DAYS_PAGES:
        regex_fact = _day_fact_from_keyword_sentences(page, SHIPPING_DELIVERY_DAYS, _DELIVERY_KEYWORDS, "shipping_time_regex")
        fact = _reconcile_one(
            SHIPPING_DELIVERY_DAYS, page, regex_fact,
            result.get("shipping_delivery_days"), result.get("shipping_delivery_source_sentence", ""), result.get("shipping_delivery_confidence", 0.0),
        )
        if fact is not None:
            facts.append(fact)

    if page.page_type in _REFUND_TERMS_PAGES:
        regex_fact = _classify_fact(page, RETURN_REFUND_TERMS, _REFUND_TERMS_RULES, "refund_terms_keyword_classifier")
        fact = _reconcile_one(
            RETURN_REFUND_TERMS, page, regex_fact,
            result.get("refund_terms"), result.get("refund_terms_source_sentence", ""), result.get("refund_terms_confidence", 0.0),
        )
        if fact is not None:
            facts.append(fact)

    return facts


def _regex_only_commerce_facts(pages: list[CrawledPage]) -> list[FactRecord]:
    """No LLM configured - exactly the same regex-only behavior this project
    had before this round, for each of the 4 facts this module now owns."""
    facts: list[FactRecord] = []
    for page in pages:
        if page.page_type in _RETURN_WINDOW_PAGES:
            fact = _return_window_fact(page)
            if fact is not None:
                facts.append(fact)
        if page.page_type in _PROCESSING_DAYS_PAGES:
            fact = _day_fact_from_keyword_sentences(page, SHIPPING_PROCESSING_DAYS, _PROCESSING_KEYWORDS, "shipping_time_regex")
            if fact is not None:
                facts.append(fact)
        if page.page_type in _DELIVERY_DAYS_PAGES:
            fact = _day_fact_from_keyword_sentences(page, SHIPPING_DELIVERY_DAYS, _DELIVERY_KEYWORDS, "shipping_time_regex")
            if fact is not None:
                facts.append(fact)
        if page.page_type in _REFUND_TERMS_PAGES:
            fact = _classify_fact(page, RETURN_REFUND_TERMS, _REFUND_TERMS_RULES, "refund_terms_keyword_classifier")
            if fact is not None:
                facts.append(fact)
    return facts


async def extract_llm_assisted_commerce_facts(site_map: SiteMap, settings: Settings, cache: LLMCache | None = None) -> list[FactRecord]:
    eligible_pages = [p for p in site_map.pages if p.reachable and (p.text or p.main_content_text) and p.page_type in _LLM_COMMERCE_FACT_PAGES]
    if not eligible_pages:
        return []

    if not settings.llm_configured:
        return _regex_only_commerce_facts(eligible_pages)

    client = get_llm_client(settings, cache)
    semaphore = asyncio.Semaphore(_LLM_CONCURRENCY)

    async def bounded(page: CrawledPage) -> dict | None:
        async with semaphore:
            return await _call_llm_for_page(client, page)

    results = await asyncio.gather(*(bounded(p) for p in eligible_pages))

    facts: list[FactRecord] = []
    for page, result in zip(eligible_pages, results):
        facts.extend(_reconcile_page(page, result))
    return facts
