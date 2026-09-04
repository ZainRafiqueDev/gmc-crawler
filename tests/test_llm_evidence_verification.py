"""Unit tests for the LLM evidence-quote fidelity check (follow-up round:
"Verify LLM Evidence-Quote Fidelity Across Every Finding"). Forced-schema/
structured output only guarantees a string sits in the right field, never
that its *content* is real page text rather than a paraphrase or a
description of what's missing - found live this round on a real
check_policy_page_substance result against leafloop.site. Closes that gap
across all four LLM-graded checks (app/llm/checks.py), never by silently
discarding a finding - only by downgrading confidence and flagging it.

The normalization tests below are checked against real page text captured
live against leafloop.site this session (via a direct browser read, not
invented for this test file) - both a genuinely real quote (the "Shipping
Fee" line) and a genuine non-quote (analytical prose the same check
produced on a different run, describing what the page is missing rather
than quoting it) - so this isn't proven only against synthetic cases.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.llm.checks import (
    _confidence_after_verification,
    _normalize_for_quote_match,
    check_claim_policy_contradiction,
    check_editorial_quality,
    check_policy_page_substance,
    check_prohibited_content,
    verify_evidence_quote,
)
from app.models import Confidence, CrawledPage, PageType, Severity

# Real page text captured live from https://leafloop.site/shipping-policy
# this session (app.checks.screenshot_annotator's live-validation round) -
# not a synthetic fixture.
_LEAFLOOP_SHIPPING_PAGE_TEXT = (
    "Shipping Destinations & Costs\n"
    "We currently ship exclusively within the United States.\n\n"
    "Shipping Area: All 50 states across the USA.\n"
    "Shipping Method: Standard Shipping\n"
    "Shipping Fee: We charge a flat standard shipping fee of $10.00 USD on all orders.\n"
    "Currency: All transactions and shipping costs are processed in USD."
)

# A real evidence_quote this exact check produced on one live run against
# that same page - genuinely verbatim, confirmed live to be a real substring.
_LEAFLOOP_REAL_QUOTE = "Shipping Fee: We charge a flat standard shipping fee of $10.00 USD on all orders."

# A real evidence_quote the SAME check produced on a DIFFERENT live run
# against the SAME page - analytical prose describing what's missing, not a
# quote of anything on the page. This is the exact live finding that
# motivated this whole follow-up round.
_LEAFLOOP_PROSE_NON_QUOTE = (
    "The shipping policy does not provide detailed information on all relevant shipping charges, "
    "handling times for various regions or services (e.g., ground, express), or a breakdown of minimum "
    "and maximum handling or transit times as required by the Google Merchant Center policy. The only "
    "stated shipping cost is a flat rate of $10 for all destinations within the USA, which does not "
    "align with the requirement to submit comprehensive shipping information for different handling "
    "and delivery services and their respective costs."
)


# --- verify_evidence_quote / _normalize_for_quote_match ---------------------

def test_real_live_quote_verifies_against_real_live_page_text():
    assert verify_evidence_quote(_LEAFLOOP_REAL_QUOTE, _LEAFLOOP_SHIPPING_PAGE_TEXT) is True


def test_real_live_prose_non_quote_does_not_verify_against_the_page_it_claims_to_describe():
    """The exact live failure that motivated this round: analytical prose
    in evidence_quote, not a real quote - must NOT verify."""
    assert verify_evidence_quote(_LEAFLOOP_PROSE_NON_QUOTE, _LEAFLOOP_SHIPPING_PAGE_TEXT) is False


def test_empty_quote_verifies_trivially():
    """No verbatim claim was made (the check legitimately fell back to its
    own reasoning text) - nothing to falsify."""
    assert verify_evidence_quote("", "any page text") is True
    assert verify_evidence_quote(None, "any page text") is True
    assert verify_evidence_quote("   ", "any page text") is True


def test_whitespace_differences_do_not_cause_a_false_negative():
    quote = "We   charge a flat\nstandard shipping fee of $10.00 USD on all orders."
    source = "Shipping Fee: We charge a flat standard shipping fee of $10.00 USD on all orders."
    assert verify_evidence_quote(quote, source) is True


def test_case_differences_do_not_cause_a_false_negative():
    quote = "WE CHARGE A FLAT STANDARD SHIPPING FEE OF $10.00 USD ON ALL ORDERS."
    assert verify_evidence_quote(quote, _LEAFLOOP_SHIPPING_PAGE_TEXT) is True


def test_html_entities_in_source_do_not_cause_a_false_negative():
    quote = "Terms & Conditions apply to all orders"
    source = "Full details: Terms &amp; Conditions apply to all orders. See below."
    assert verify_evidence_quote(quote, source) is True


def test_genuinely_fabricated_quote_does_not_verify():
    assert verify_evidence_quote("We offer same-day delivery worldwide for free.", _LEAFLOOP_SHIPPING_PAGE_TEXT) is False


def test_normalize_collapses_whitespace_decodes_entities_and_lowercases():
    assert _normalize_for_quote_match("  Foo   Bar &amp; Baz\n\n") == "foo bar & baz"


# --- _confidence_after_verification -----------------------------------------

def test_confirmed_downgrades_to_potential_risk_when_not_verified():
    assert _confidence_after_verification(Confidence.CONFIRMED, verified=False) == Confidence.POTENTIAL_RISK


def test_confirmed_stays_confirmed_when_verified():
    assert _confidence_after_verification(Confidence.CONFIRMED, verified=True) == Confidence.CONFIRMED


def test_potential_risk_stays_potential_risk_when_not_verified():
    assert _confidence_after_verification(Confidence.POTENTIAL_RISK, verified=False) == Confidence.POTENTIAL_RISK


# --- Integration: applied across all four LLM-graded checks -----------------

class FakeClaudeClient:
    def __init__(self, responses: list[dict | None]):
        self._responses = list(responses)

    async def call_tool(self, system: str, user: str, tool_name: str, tool_schema: dict, max_tokens: int = 1024):
        return self._responses.pop(0)


def _page(url: str, page_type: PageType, text: str) -> CrawledPage:
    return CrawledPage(url=url, page_type=page_type, depth=1, reachable=True, text=text, html=f"<html><body>{text}</body></html>")


_SETTINGS = Settings(llm_provider="claude", anthropic_api_key="fake-key-for-tests")


@pytest.mark.asyncio
async def test_policy_substance_real_quote_is_marked_verified_and_keeps_confirmed():
    client = FakeClaudeClient([{
        "meets_requirement": False, "confidence": "confirmed", "evidence_quote": _LEAFLOOP_REAL_QUOTE,
        "location": "shipping fees section", "reasoning": "Doesn't cover handling times.", "recommended_fix": "Add handling times.",
    }])
    page = _page("https://leafloop.site/shipping-policy", PageType.SHIPPING_POLICY, _LEAFLOOP_SHIPPING_PAGE_TEXT)
    finding = await check_policy_page_substance(client, page, "shipping_policy", _SETTINGS)
    assert finding is not None
    assert finding.evidence_verified is True
    assert finding.confidence == Confidence.CONFIRMED


@pytest.mark.asyncio
async def test_policy_substance_prose_non_quote_is_flagged_and_downgraded():
    """The exact live scenario that motivated this round, reproduced as a
    deterministic unit test: the model returns analytical prose in
    evidence_quote instead of a real quote."""
    client = FakeClaudeClient([{
        "meets_requirement": False, "confidence": "confirmed", "evidence_quote": _LEAFLOOP_PROSE_NON_QUOTE,
        "location": "shipping policy section", "reasoning": "Missing handling-time detail.", "recommended_fix": "Add handling times.",
    }])
    page = _page("https://leafloop.site/shipping-policy", PageType.SHIPPING_POLICY, _LEAFLOOP_SHIPPING_PAGE_TEXT)
    finding = await check_policy_page_substance(client, page, "shipping_policy", _SETTINGS)
    assert finding is not None
    assert finding.evidence_verified is False
    assert finding.confidence == Confidence.POTENTIAL_RISK  # downgraded from the model's own "confirmed"
    assert finding.evidence == _LEAFLOOP_PROSE_NON_QUOTE  # the finding itself is still reported, never discarded


@pytest.mark.asyncio
async def test_policy_substance_empty_evidence_quote_falls_back_to_reasoning_and_stays_verified():
    """A legitimate schema-allowed case (no quote to give) - the check's
    own fallback to reasoning text makes no verbatim claim, so nothing to
    flag; evidence_verified must stay True, not False."""
    client = FakeClaudeClient([{
        "meets_requirement": False, "confidence": "confirmed", "evidence_quote": "",
        "location": "shipping policy section", "reasoning": "No shipping fee stated anywhere on the page.", "recommended_fix": "Add a shipping fee.",
    }])
    page = _page("https://x.example/shipping-policy", PageType.SHIPPING_POLICY, "Some unrelated policy text.")
    finding = await check_policy_page_substance(client, page, "shipping_policy", _SETTINGS)
    assert finding is not None
    assert finding.evidence_verified is True
    assert finding.confidence == Confidence.CONFIRMED
    assert finding.evidence == "No shipping fee stated anywhere on the page."


@pytest.mark.asyncio
async def test_editorial_quality_fabricated_quote_is_flagged_and_downgraded():
    client = FakeClaudeClient([{
        "has_quality_issue": True, "confidence": "confirmed", "evidence_quote": "This text does not appear anywhere on the page.",
        "location": "main product description", "issue_description": "Generic copy.", "recommended_fix": "Rewrite.",
    }])
    page = _page("https://x.example/", PageType.HOMEPAGE, "Welcome to our store. We sell quality ceramic pots.")
    finding = await check_editorial_quality(client, page, _SETTINGS)
    assert finding is not None
    assert finding.evidence_verified is False
    assert finding.confidence == Confidence.POTENTIAL_RISK


@pytest.mark.asyncio
async def test_editorial_quality_real_quote_stays_verified():
    client = FakeClaudeClient([{
        "has_quality_issue": True, "confidence": "confirmed", "evidence_quote": "Lorem ipsum dolor sit amet",
        "location": "main product description", "issue_description": "Placeholder text left on live page.", "recommended_fix": "Replace with real copy.",
    }])
    page = _page("https://x.example/", PageType.HOMEPAGE, "Welcome to our store. Lorem ipsum dolor sit amet, consectetur.")
    finding = await check_editorial_quality(client, page, _SETTINGS)
    assert finding is not None
    assert finding.evidence_verified is True
    assert finding.confidence == Confidence.CONFIRMED


@pytest.mark.asyncio
async def test_prohibited_content_fabricated_quote_is_flagged_and_downgraded():
    client = FakeClaudeClient([{
        "potentially_prohibited": True, "confidence": "confirmed", "matched_category": "counterfeit",
        "evidence_quote": "AAA mirror quality replica - not actually on this page",
        "location": "product description", "reasoning": "Suspicious language.",
    }])
    page = _page("https://x.example/product/watch", PageType.PRODUCT, "A stylish, high-quality watch for everyday wear.")
    finding = await check_prohibited_content(client, page, _SETTINGS)
    assert finding is not None
    assert finding.evidence_verified is False
    assert finding.confidence == Confidence.POTENTIAL_RISK


@pytest.mark.asyncio
async def test_prohibited_content_real_quote_stays_verified():
    client = FakeClaudeClient([{
        "potentially_prohibited": True, "confidence": "confirmed", "matched_category": "counterfeit",
        "evidence_quote": "AAA quality 1:1 mirror replica",
        "location": "product description", "reasoning": "Explicit replica/counterfeit language.",
    }])
    page = _page("https://x.example/product/watch", PageType.PRODUCT, "Luxury watch, AAA quality 1:1 mirror replica, ships fast.")
    finding = await check_prohibited_content(client, page, _SETTINGS)
    assert finding is not None
    assert finding.evidence_verified is True
    assert finding.confidence == Confidence.CONFIRMED


@pytest.mark.asyncio
async def test_claim_contradiction_both_real_quotes_stays_verified():
    client = FakeClaudeClient([{
        "has_contradiction": True, "confidence": "confirmed", "conflict_dimension": "cost",
        "claim_quote": "Enjoy free shipping on every order!", "policy_quote": "A flat shipping fee of $7.99 applies to all orders.",
        "location": "homepage hero banner", "reasoning": "Free vs flat fee.", "recommended_fix": "Align claim with policy.",
    }])
    claim_page = _page("https://x.example/", PageType.HOMEPAGE, "Enjoy free shipping on every order! Shop now.")
    policy_page = _page("https://x.example/shipping-policy", PageType.SHIPPING_POLICY, "A flat shipping fee of $7.99 applies to all orders.")
    finding = await check_claim_policy_contradiction(client, claim_page, policy_page, "shipping", _SETTINGS)
    assert finding is not None
    assert finding.evidence_verified is True
    assert finding.confidence == Confidence.CONFIRMED


@pytest.mark.asyncio
async def test_claim_contradiction_claim_quote_not_on_claim_page_is_flagged():
    """claim_quote must be verified against the CLAIM page specifically -
    real text that only exists on the policy page (or nowhere at all)
    still fails, even though policy_quote itself verifies fine."""
    client = FakeClaudeClient([{
        "has_contradiction": True, "confidence": "confirmed", "conflict_dimension": "cost",
        "claim_quote": "This exact phrase is not on the claim page.", "policy_quote": "A flat shipping fee of $7.99 applies to all orders.",
        "location": "homepage hero banner", "reasoning": "Free vs flat fee.", "recommended_fix": "Align claim with policy.",
    }])
    claim_page = _page("https://x.example/", PageType.HOMEPAGE, "Enjoy free shipping on every order! Shop now.")
    policy_page = _page("https://x.example/shipping-policy", PageType.SHIPPING_POLICY, "A flat shipping fee of $7.99 applies to all orders.")
    finding = await check_claim_policy_contradiction(client, claim_page, policy_page, "shipping", _SETTINGS)
    assert finding is not None
    assert finding.evidence_verified is False
    assert finding.confidence == Confidence.POTENTIAL_RISK


@pytest.mark.asyncio
async def test_claim_contradiction_policy_quote_not_on_policy_page_is_flagged():
    client = FakeClaudeClient([{
        "has_contradiction": True, "confidence": "confirmed", "conflict_dimension": "cost",
        "claim_quote": "Enjoy free shipping on every order!", "policy_quote": "This exact phrase is not on the policy page.",
        "location": "homepage hero banner", "reasoning": "Free vs flat fee.", "recommended_fix": "Align claim with policy.",
    }])
    claim_page = _page("https://x.example/", PageType.HOMEPAGE, "Enjoy free shipping on every order! Shop now.")
    policy_page = _page("https://x.example/shipping-policy", PageType.SHIPPING_POLICY, "A flat shipping fee of $7.99 applies to all orders.")
    finding = await check_claim_policy_contradiction(client, claim_page, policy_page, "shipping", _SETTINGS)
    assert finding is not None
    assert finding.evidence_verified is False
    assert finding.confidence == Confidence.POTENTIAL_RISK


# --- Report rendering: the note appears only when verification failed -------

def test_report_shows_note_only_for_unverified_findings():
    from app.report import _format_finding, _format_finding_rich

    from app.models import Finding

    verified_finding = Finding(
        check_id="llm_policy_substance_shipping_policy", title="Shipping policy page lacks required substance",
        severity=Severity.HIGH, confidence=Confidence.CONFIRMED, page_url="https://x.example/shipping-policy",
        evidence=_LEAFLOOP_REAL_QUOTE, evidence_verified=True,
    )
    unverified_finding = verified_finding.model_copy(update={"confidence": Confidence.POTENTIAL_RISK, "evidence": _LEAFLOOP_PROSE_NON_QUOTE, "evidence_verified": False})

    assert "could not be independently verified" not in _format_finding(verified_finding)
    assert "could not be independently verified" in _format_finding(unverified_finding)
    assert "could not be independently verified" not in _format_finding_rich(verified_finding)
    assert "could not be independently verified" in _format_finding_rich(unverified_finding)
