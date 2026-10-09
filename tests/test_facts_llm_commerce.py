"""Tests for app.facts.llm_commerce_facts - LLM-assisted extraction for the
four "free-prose" commerce facts (return.window_days, shipping.processing_days,
shipping.delivery_days, return.refund_terms). Regex keyword-proximity cannot
reliably tell what a number refers to - confirmed live, twice, on
modcloth.com: "processed within 2 business days" is refund-processing speed,
not the return window, and "Requests must be opened within 30 days..." is a
real return-window statement that never uses the literal word "return".

The fake LLM client dispatches by the page URL embedded in the user prompt
(there is only one tool, "submit_commerce_facts", called once per eligible
page) rather than a shared FIFO queue - same reasoning as
test_soft_404_llm_suppression.py's _FakeClaudeClient: app.facts.
llm_commerce_facts.extract_llm_assisted_commerce_facts runs one call per
page concurrently (asyncio.gather), so a FIFO queue's response-to-call
mapping would be non-deterministic.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.facts import llm_commerce_facts
from app.facts.keys import RETURN_REFUND_TERMS, RETURN_WINDOW_DAYS, SHIPPING_DELIVERY_DAYS, SHIPPING_PROCESSING_DAYS
from app.facts.llm_commerce_facts import extract_llm_assisted_commerce_facts
from app.models import CrawledPage, PageType, SiteMap

_SETTINGS_LLM_ON = Settings(anthropic_api_key="fake-key-for-tests")
_SETTINGS_LLM_OFF = Settings(anthropic_api_key="", openai_api_key="")

# Real modcloth.com text shapes (confirmed live) - abbreviated but faithful.
_RETURN_POLICY_TEXT = (
    "The original plastic bag with product barcode must be included with your return. "
    "Refunds: You may choose to receive a full refund to your original payment method which will be "
    "processed within 2 business days of your return's arrival to our US based warehouse."
)
_SHIPPING_RETURNS_TEXT = (
    "Final sale items are not returnable. "
    "The original plastic bag with product barcode must be included with your return. "
    "Requests must be opened in our Loop portal within 30 days of the order's original delivery date "
    "to be eligible for refund."
)
_FINAL_SALE_ONLY_TEXT = "Final sale items are not returnable. We do not accept exchanges either."


def _facts_of(facts, key):
    return [f for f in facts if f.fact == key]


def _site_map_with(*pages: CrawledPage) -> SiteMap:
    return SiteMap(base_url="https://example.com", pages=list(pages))


def _returns_page(url: str, text: str) -> CrawledPage:
    return CrawledPage(url=url, page_type=PageType.RETURNS_POLICY, depth=1, text=text, status=200)


class _FakeCommerceFactsClient:
    def __init__(self, responses_by_url: dict[str, dict]):
        self._responses_by_url = responses_by_url
        self.calls: list[str] = []

    async def call_tool(self, system, user, tool_name, tool_schema, max_tokens=1024):
        assert tool_name == "submit_commerce_facts"
        for url, response in self._responses_by_url.items():
            if f"Page URL: {url}" in user:
                self.calls.append(url)
                return response
        raise AssertionError(f"no fake response configured for this call - user prompt starts: {user[:200]!r}")


_NULL_RESPONSE = {
    "return_window_days": None, "return_window_source_sentence": "", "return_window_confidence": 0.0,
    "shipping_processing_days": None, "shipping_processing_source_sentence": "", "shipping_processing_confidence": 0.0,
    "shipping_delivery_days": None, "shipping_delivery_source_sentence": "", "shipping_delivery_confidence": 0.0,
    "refund_terms": None, "refund_terms_source_sentence": "", "refund_terms_confidence": 0.0,
}


def _response(**overrides) -> dict:
    merged = dict(_NULL_RESPONSE)
    merged.update(overrides)
    return merged


def _patch_client(monkeypatch, fake_client) -> None:
    monkeypatch.setattr(llm_commerce_facts, "get_llm_client", lambda settings, cache=None: fake_client)


# --- No LLM configured: exact pre-existing regex-only behavior -------------

@pytest.mark.asyncio
async def test_no_llm_configured_falls_back_to_pure_regex_behavior():
    page = _returns_page("https://example.com/returns", "You may return any item within 30 days of purchase.")
    facts = await extract_llm_assisted_commerce_facts(_site_map_with(page), _SETTINGS_LLM_OFF)
    assert _facts_of(facts, RETURN_WINDOW_DAYS)[0].value == "30"
    assert _facts_of(facts, RETURN_WINDOW_DAYS)[0].method == "return_window_regex"


@pytest.mark.asyncio
async def test_no_llm_configured_processing_vs_delivery_still_distinguished():
    page = CrawledPage(
        url="https://example.com/shipping", page_type=PageType.SHIPPING_POLICY, depth=1,
        text="Order processing time is 1-2 business days. Delivery time is 3-5 business days.",
    )
    facts = await extract_llm_assisted_commerce_facts(_site_map_with(page), _SETTINGS_LLM_OFF)
    assert _facts_of(facts, SHIPPING_PROCESSING_DAYS)[0].value == "2"
    assert _facts_of(facts, SHIPPING_DELIVERY_DAYS)[0].value == "5"


@pytest.mark.asyncio
async def test_no_llm_configured_refund_terms_still_classified():
    page = _returns_page("https://example.com/returns", "Returned items are eligible for store credit only.")
    facts = await extract_llm_assisted_commerce_facts(_site_map_with(page), _SETTINGS_LLM_OFF)
    assert _facts_of(facts, RETURN_REFUND_TERMS)[0].value == "store_credit_only"


# --- The two real modcloth.com false-positive scenarios, LLM-assisted -----

@pytest.mark.asyncio
async def test_refund_processing_time_is_not_extracted_as_return_window(monkeypatch):
    """The regex fast-path alone would extract "2" (confirmed live, a real
    bug) - the LLM correctly recognizes this page states no general return
    window at all (only refund-processing speed), so nothing is emitted."""
    url = "https://modcloth.com/pages/return-policy"
    page = _returns_page(url, _RETURN_POLICY_TEXT)
    fake = _FakeCommerceFactsClient({url: _response()})  # LLM: nothing stated here
    _patch_client(monkeypatch, fake)

    facts = await extract_llm_assisted_commerce_facts(_site_map_with(page), _SETTINGS_LLM_ON)

    assert _facts_of(facts, RETURN_WINDOW_DAYS) == []  # never "2"
    assert fake.calls == [url]


@pytest.mark.asyncio
async def test_window_stated_without_the_word_return_nearby_is_still_extracted(monkeypatch):
    """The regex fast-path alone would extract "0" (final-sale fallback,
    confirmed live) because the real "30 days" sentence never contains the
    literal word "return". The LLM reads for meaning and finds it."""
    url = "https://modcloth.com/pages/shipping-returns"
    page = _returns_page(url, _SHIPPING_RETURNS_TEXT)
    source_sentence = "Requests must be opened in our Loop portal within 30 days of the order's original delivery date to be eligible for refund."
    fake = _FakeCommerceFactsClient({
        url: _response(return_window_days=30, return_window_source_sentence=source_sentence, return_window_confidence=0.9),
    })
    _patch_client(monkeypatch, fake)

    facts = await extract_llm_assisted_commerce_facts(_site_map_with(page), _SETTINGS_LLM_ON)

    window_facts = _facts_of(facts, RETURN_WINDOW_DAYS)
    assert len(window_facts) == 1
    assert window_facts[0].value == "30"
    assert window_facts[0].method == "llm"  # regex disagreed (its own candidate was "0") - LLM's value wins
    assert window_facts[0].confidence == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_final_sale_only_page_stays_correct(monkeypatch):
    """The existing final-sale case must stay correct: a genuine no-returns
    page still yields 0 - and since the LLM and regex now AGREE on this
    (both recognize "no returns at all"), it comes back as the high-
    confidence regex_llm_agree method."""
    url = "https://example.com/final-sale-only"
    page = _returns_page(url, _FINAL_SALE_ONLY_TEXT)
    fake = _FakeCommerceFactsClient({
        url: _response(return_window_days=0, return_window_source_sentence="Final sale items are not returnable.", return_window_confidence=0.9),
    })
    _patch_client(monkeypatch, fake)

    facts = await extract_llm_assisted_commerce_facts(_site_map_with(page), _SETTINGS_LLM_ON)

    window_facts = _facts_of(facts, RETURN_WINDOW_DAYS)
    assert len(window_facts) == 1
    assert window_facts[0].value == "0"
    assert window_facts[0].method == "regex_llm_agree"
    assert window_facts[0].confidence == pytest.approx(0.95)


# --- Reconciliation policy, directly ----------------------------------------

@pytest.mark.asyncio
async def test_llm_says_nothing_stated_emits_no_fact_even_if_regex_found_something(monkeypatch):
    url = "https://example.com/returns"
    page = _returns_page(url, "You may return any item within 30 days of purchase.")  # regex WOULD find "30"
    fake = _FakeCommerceFactsClient({url: _response()})  # LLM: null
    _patch_client(monkeypatch, fake)

    facts = await extract_llm_assisted_commerce_facts(_site_map_with(page), _SETTINGS_LLM_ON)
    assert _facts_of(facts, RETURN_WINDOW_DAYS) == []


@pytest.mark.asyncio
async def test_hallucinated_source_sentence_is_discarded(monkeypatch):
    url = "https://example.com/returns"
    page = _returns_page(url, "You may return any item within 30 days of purchase.")
    fake = _FakeCommerceFactsClient({
        url: _response(return_window_days=45, return_window_source_sentence="This sentence does not appear on the page anywhere.", return_window_confidence=0.9),
    })
    _patch_client(monkeypatch, fake)

    facts = await extract_llm_assisted_commerce_facts(_site_map_with(page), _SETTINGS_LLM_ON)
    assert _facts_of(facts, RETURN_WINDOW_DAYS) == []  # unverifiable quote - never trusted


@pytest.mark.asyncio
async def test_llm_confidence_is_clamped_to_valid_range(monkeypatch):
    url = "https://example.com/returns"
    page = _returns_page(url, "No general return window is stated on this page at all, just general prose.")
    fake = _FakeCommerceFactsClient({
        url: _response(return_window_days=14, return_window_source_sentence="general prose", return_window_confidence=1.7),
    })
    _patch_client(monkeypatch, fake)

    facts = await extract_llm_assisted_commerce_facts(_site_map_with(page), _SETTINGS_LLM_ON)
    assert _facts_of(facts, RETURN_WINDOW_DAYS)[0].confidence == 1.0


@pytest.mark.asyncio
async def test_disagreement_uses_llm_value_not_regex_value(monkeypatch):
    url = "https://example.com/returns"
    page = _returns_page(url, "You may return any item within 30 days of purchase.")  # regex finds "30"
    fake = _FakeCommerceFactsClient({
        url: _response(return_window_days=14, return_window_source_sentence="within 30 days of purchase", return_window_confidence=0.6),
    })
    _patch_client(monkeypatch, fake)

    facts = await extract_llm_assisted_commerce_facts(_site_map_with(page), _SETTINGS_LLM_ON)
    window_facts = _facts_of(facts, RETURN_WINDOW_DAYS)
    assert window_facts[0].value == "14"  # LLM wins on disagreement, not regex's "30"
    assert window_facts[0].method == "llm"
    assert window_facts[0].confidence == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_multiple_pages_each_get_their_own_fake_response(monkeypatch):
    """Confirms dispatch is per-page, not a shared FIFO - both real modcloth
    pages processed in the same call, each getting its own correct answer."""
    return_policy_url = "https://modcloth.com/pages/return-policy"
    shipping_returns_url = "https://modcloth.com/pages/shipping-returns"
    pages = [
        _returns_page(return_policy_url, _RETURN_POLICY_TEXT),
        _returns_page(shipping_returns_url, _SHIPPING_RETURNS_TEXT),
    ]
    fake = _FakeCommerceFactsClient({
        return_policy_url: _response(),
        shipping_returns_url: _response(return_window_days=30, return_window_source_sentence="Requests must be opened in our Loop portal within 30 days of the order's original delivery date to be eligible for refund.", return_window_confidence=0.9),
    })
    _patch_client(monkeypatch, fake)

    facts = await extract_llm_assisted_commerce_facts(_site_map_with(*pages), _SETTINGS_LLM_ON)

    window_facts = _facts_of(facts, RETURN_WINDOW_DAYS)
    assert len(window_facts) == 1  # only the shipping-returns page produced one
    assert window_facts[0].value == "30"
    assert sorted(fake.calls) == sorted([return_policy_url, shipping_returns_url])
