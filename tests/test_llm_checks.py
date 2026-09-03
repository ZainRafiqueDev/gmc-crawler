"""Unit tests for the LLM-graded checks (app/llm/checks.py). The Claude API
is never called - a fake client returns canned tool-call results so these
tests are fast, free, and deterministic.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.llm.checks import (
    _product_sample_size,
    _select_product_sample,
    check_claim_policy_contradiction,
    check_editorial_quality,
    check_policy_page_substance,
    check_prohibited_content,
    run_llm_checks,
)
from app.models import Confidence, CrawledPage, PageType, Severity, SiteMap


class FakeClaudeClient:
    def __init__(self, responses: list[dict | None]):
        self._responses = list(responses)
        self.calls: list[tuple[str, str, str]] = []

    async def call_tool(self, system: str, user: str, tool_name: str, tool_schema: dict, max_tokens: int = 1024):
        self.calls.append((system, user, tool_name))
        return self._responses.pop(0)


def _page(url: str, page_type: PageType, text: str) -> CrawledPage:
    return CrawledPage(url=url, page_type=page_type, depth=1, reachable=True, text=text, html=f"<html><body>{text}</body></html>")


# db=None/cache=None (the defaults) makes get_policy_context fall back to
# the hand-written stub snippet (app/llm/policy_snippets.py) - exactly the
# grounding text these tests were already written against, so no DB/cache
# fixture is needed here. Real RAG retrieval is covered in
# tests/test_policy_rag.py instead.
_SETTINGS = Settings(llm_provider="claude", anthropic_api_key="fake-key-for-tests")


@pytest.mark.asyncio
async def test_policy_substance_no_finding_when_requirement_met():
    client = FakeClaudeClient([{"meets_requirement": True, "confidence": "confirmed", "evidence_quote": "Returns accepted within 30 days.", "reasoning": "ok", "recommended_fix": ""}])
    page = _page("https://x.example/returns", PageType.RETURNS_POLICY, "Returns accepted within 30 days of purchase, contact support to start a return.")
    finding = await check_policy_page_substance(client, page, "returns_refunds", _SETTINGS)
    assert finding is None


@pytest.mark.asyncio
async def test_policy_substance_finding_when_requirement_not_met_cites_policy_and_real_evidence():
    client = FakeClaudeClient([{
        "meets_requirement": False,
        "confidence": "confirmed",
        "evidence_quote": "We accept returns.",
        "location": "returns policy section, first paragraph",
        "reasoning": "No timeframe or process specified.",
        "recommended_fix": "State a concrete return window and process.",
    }])
    page = _page("https://x.example/returns", PageType.RETURNS_POLICY, "We accept returns.")
    finding = await check_policy_page_substance(client, page, "returns_refunds", _SETTINGS)
    assert finding is not None
    assert finding.severity == Severity.HIGH
    assert finding.confidence == Confidence.CONFIRMED
    assert finding.evidence == "We accept returns."
    assert "returns_refunds" in finding.policy_reference
    assert finding.recommended_fix == "State a concrete return window and process."
    assert finding.location == "returns policy section, first paragraph"
    assert finding.detected_at is not None


@pytest.mark.asyncio
async def test_policy_substance_cannot_verify_when_llm_call_fails():
    client = FakeClaudeClient([None])
    page = _page("https://x.example/returns", PageType.RETURNS_POLICY, "We accept returns.")
    finding = await check_policy_page_substance(client, page, "returns_refunds", _SETTINGS)
    assert finding is not None
    assert finding.confidence == Confidence.CANNOT_VERIFY


@pytest.mark.asyncio
async def test_editorial_quality_flags_issue():
    client = FakeClaudeClient([{
        "has_quality_issue": True,
        "confidence": "potential_risk",
        "evidence_quote": "Lorem ipsum dolor sit amet",
        "issue_description": "Placeholder text left on live page",
        "recommended_fix": "Replace placeholder text with real copy.",
    }])
    page = _page("https://x.example/", PageType.HOMEPAGE, "Welcome. Lorem ipsum dolor sit amet.")
    finding = await check_editorial_quality(client, page, _SETTINGS)
    assert finding is not None
    assert finding.confidence == Confidence.POTENTIAL_RISK
    assert "Lorem ipsum" in finding.evidence


@pytest.mark.asyncio
async def test_prohibited_content_flags_and_is_critical():
    client = FakeClaudeClient([{
        "potentially_prohibited": True,
        "confidence": "potential_risk",
        "matched_category": "weapons",
        "evidence_quote": "Fully functional replica firearm",
        "reasoning": "Describes a weapon replica",
    }])
    page = _page("https://x.example/product/replica", PageType.PRODUCT, "Fully functional replica firearm, ships worldwide.")
    finding = await check_prohibited_content(client, page, _SETTINGS)
    assert finding is not None
    assert finding.severity == Severity.CRITICAL
    assert "weapons" in finding.title


@pytest.mark.asyncio
async def test_prohibited_content_no_finding_when_clean():
    client = FakeClaudeClient([{
        "potentially_prohibited": False,
        "confidence": "confirmed",
        "matched_category": "",
        "evidence_quote": "",
        "reasoning": "Ordinary product, no concerns.",
    }])
    page = _page("https://x.example/product/mug", PageType.PRODUCT, "Ceramic coffee mug, 12oz, dishwasher safe.")
    finding = await check_prohibited_content(client, page, _SETTINGS)
    assert finding is None


@pytest.mark.asyncio
async def test_prohibited_content_prompt_includes_counterfeit_guidance():
    """Follow-up round ("separately" section): the counterfeit/brand-risk
    watch-term list and the "brand mention alone is not evidence" rule are
    prompt guidance folded into the existing llm_prohibited_content check,
    not a new check/category. Structural regression guard that the guidance
    is actually wired into the system prompt sent to the model - the real
    behavioral confirmation (a mere brand mention isn't flagged, genuine
    replica language is) needs a real LLM call, validated live separately,
    not simulable with a fake client that just echoes canned responses."""
    client = FakeClaudeClient([{
        "potentially_prohibited": False, "confidence": "confirmed", "matched_category": "", "evidence_quote": "", "reasoning": "",
    }])
    page = _page("https://x.example/product/case", PageType.PRODUCT, "Phone case, compatible with iPhone 14.")
    await check_prohibited_content(client, page, _SETTINGS)
    system_prompt = client.calls[0][0]
    assert "brand name appearing on the page is NOT by itself evidence" in system_prompt
    assert "replica" in system_prompt and "1:1" in system_prompt and "AAA" in system_prompt


# --- Part 3 of the follow-up round: claim-vs-policy contradiction check --

@pytest.mark.asyncio
async def test_claim_contradiction_flags_genuine_disagreement_with_both_quotes():
    client = FakeClaudeClient([{
        "has_contradiction": True,
        "confidence": "confirmed",
        "conflict_dimension": "cost",
        "claim_quote": "Enjoy free shipping on every order!",
        "policy_quote": "A flat shipping fee of $7.99 applies to all orders.",
        "location": "homepage hero banner",
        "reasoning": "Homepage promises free shipping; policy states a flat fee with no free-shipping threshold.",
        "recommended_fix": "Either offer free shipping as claimed or update the homepage banner to match the flat-fee policy.",
    }])
    claim_page = _page("https://x.example/", PageType.HOMEPAGE, "Enjoy free shipping on every order! Shop now.")
    policy_page = _page("https://x.example/shipping-policy", PageType.SHIPPING_POLICY, "A flat shipping fee of $7.99 applies to all orders.")
    finding = await check_claim_policy_contradiction(client, claim_page, policy_page, "shipping", _SETTINGS)
    assert finding is not None
    assert finding.check_id == "llm_claim_policy_contradiction"
    assert finding.severity == Severity.HIGH
    assert finding.confidence == Confidence.CONFIRMED
    assert "cost" in finding.title
    assert "Enjoy free shipping on every order!" in finding.evidence
    assert "$7.99" in finding.evidence
    assert finding.page_url == "https://x.example/"


@pytest.mark.asyncio
async def test_claim_contradiction_no_finding_when_llm_finds_none():
    client = FakeClaudeClient([{
        "has_contradiction": False, "confidence": "confirmed", "conflict_dimension": "none", "claim_quote": "", "policy_quote": "",
        "location": "", "reasoning": "The claim is consistent with the policy.", "recommended_fix": "",
    }])
    claim_page = _page("https://x.example/product/mug", PageType.PRODUCT, "See our shipping policy for details.")
    policy_page = _page("https://x.example/shipping-policy", PageType.SHIPPING_POLICY, "We ship within 3-5 business days.")
    finding = await check_claim_policy_contradiction(client, claim_page, policy_page, "shipping", _SETTINGS)
    assert finding is None


@pytest.mark.asyncio
async def test_claim_contradiction_discarded_when_conflict_dimension_is_none_despite_has_contradiction_true():
    """Hardened after live testing found the model could say
    has_contradiction=True on a verdict that didn't actually name a real
    conflict (ordinary policy exceptions on an otherwise-matching claim,
    or a policy quote that actually agreed with the claim) - the
    structured conflict_dimension field is a second gate on top of the
    bare boolean, and "none" here must discard the finding regardless of
    what has_contradiction says."""
    client = FakeClaudeClient([{
        "has_contradiction": True, "confidence": "confirmed", "conflict_dimension": "none",
        "claim_quote": "30-Day Free Returns", "policy_quote": "Used, damaged, or missing-parts items may be denied a refund.",
        "location": "product page badge", "reasoning": "Just ordinary exceptions, not a real conflict.", "recommended_fix": "",
    }])
    claim_page = _page("https://x.example/product/widget", PageType.PRODUCT, "30-Day Free Returns on all orders.")
    policy_page = _page("https://x.example/returns-policy", PageType.RETURNS_POLICY, "30-day returns. Used, damaged, or missing-parts items may be denied a refund.")
    finding = await check_claim_policy_contradiction(client, claim_page, policy_page, "returns", _SETTINGS)
    assert finding is None


@pytest.mark.asyncio
async def test_claim_contradiction_discarded_when_both_quotes_name_the_same_day_count():
    """Deterministic backstop, added after prompt hardening alone proved
    insufficient live: reproduced on 5 of 5 real product pages on a real
    store, gpt-4o-mini verdicted a "timeframe_or_window" conflict for
    "30-Day Free Returns" against a policy quote that ALSO said "30-day
    return window" - the model's own quoted evidence agreed with the
    claim; only surrounding exception language differed. When both quotes
    name the identical day count and the claimed dimension is
    timeframe_or_window, discard regardless of what the model concluded."""
    client = FakeClaudeClient([{
        "has_contradiction": True, "confidence": "confirmed", "conflict_dimension": "timeframe_or_window",
        "claim_quote": "30-Day Free Returns",
        "policy_quote": "If an item is returned to us used, damaged, or outside of the 30-day return window, we reserve the right to deny the refund.",
        "location": "product page badge", "reasoning": "Claims 30 days, policy also says 30 days but with exceptions.",
        "recommended_fix": "",
    }])
    claim_page = _page("https://x.example/product/widget", PageType.PRODUCT, "30-Day Free Returns on all orders.")
    policy_page = _page("https://x.example/returns-policy", PageType.RETURNS_POLICY, "30-day return window. Used or damaged items may be denied a refund.")
    finding = await check_claim_policy_contradiction(client, claim_page, policy_page, "returns", _SETTINGS)
    assert finding is None


@pytest.mark.asyncio
async def test_claim_contradiction_not_discarded_when_day_counts_genuinely_differ():
    """The day-count guard must not suppress a REAL timeframe conflict -
    only fires when both quotes name the SAME day count."""
    client = FakeClaudeClient([{
        "has_contradiction": True, "confidence": "confirmed", "conflict_dimension": "timeframe_or_window",
        "claim_quote": "30-Day Free Returns",
        "policy_quote": "Returns are only accepted within 7 days of delivery.",
        "location": "product page badge", "reasoning": "Claims 30 days, policy states a 7-day window - a real conflict.",
        "recommended_fix": "Update the product page to match the 7-day policy, or extend the policy to 30 days.",
    }])
    claim_page = _page("https://x.example/product/widget", PageType.PRODUCT, "30-Day Free Returns on all orders.")
    policy_page = _page("https://x.example/returns-policy", PageType.RETURNS_POLICY, "Returns are only accepted within 7 days of delivery.")
    finding = await check_claim_policy_contradiction(client, claim_page, policy_page, "returns", _SETTINGS)
    assert finding is not None
    assert "30-Day Free Returns" in finding.evidence
    assert "7 days" in finding.evidence


@pytest.mark.asyncio
async def test_claim_contradiction_discarded_if_either_quote_is_missing():
    """Anti-hallucination gate: a "contradiction" verdict without a real
    verbatim quote on BOTH sides is not trustworthy enough to report, even
    if the model says has_contradiction=True."""
    client = FakeClaudeClient([{
        "has_contradiction": True, "confidence": "confirmed", "conflict_dimension": "cost", "claim_quote": "Free shipping!", "policy_quote": "",
        "location": "homepage", "reasoning": "seems inconsistent", "recommended_fix": "",
    }])
    claim_page = _page("https://x.example/", PageType.HOMEPAGE, "Free shipping!")
    policy_page = _page("https://x.example/shipping-policy", PageType.SHIPPING_POLICY, "Shipping details vary by destination.")
    finding = await check_claim_policy_contradiction(client, claim_page, policy_page, "shipping", _SETTINGS)
    assert finding is None


@pytest.mark.asyncio
async def test_claim_contradiction_prompt_topic_locks_to_the_given_claim_type():
    """Live testing found a topic mismatch (a shipping claim quoted as
    "contradicting" a returns policy page, or vice versa) when the prompt
    didn't explicitly scope which claim type to evaluate - confirm the
    claim_type-specific instruction is actually in the prompt sent to the
    model."""
    client = FakeClaudeClient([{
        "has_contradiction": False, "confidence": "confirmed", "conflict_dimension": "none", "claim_quote": "", "policy_quote": "",
        "location": "", "reasoning": "", "recommended_fix": "",
    }])
    claim_page = _page("https://x.example/", PageType.HOMEPAGE, "Free shipping! 30-Day Returns!")
    policy_page = _page("https://x.example/returns-policy", PageType.RETURNS_POLICY, "30-day returns accepted.")
    await check_claim_policy_contradiction(client, claim_page, policy_page, "returns", _SETTINGS)
    system_prompt = client.calls[0][0]
    assert "Only evaluate returns/refund claims" in system_prompt
    assert "ignore any other kind of claim" in system_prompt


@pytest.mark.asyncio
async def test_claim_contradiction_skipped_when_either_page_has_no_text():
    client = FakeClaudeClient([])
    claim_page = CrawledPage(url="https://x.example/", page_type=PageType.HOMEPAGE, depth=0, reachable=True, text="")
    policy_page = _page("https://x.example/shipping-policy", PageType.SHIPPING_POLICY, "We ship within 3-5 business days.")
    finding = await check_claim_policy_contradiction(client, claim_page, policy_page, "shipping", _SETTINGS)
    assert finding is None
    assert client.calls == []  # never even called the LLM with nothing to compare


@pytest.mark.asyncio
async def test_claim_contradiction_tasks_empty_when_no_policy_page_exists():
    """Nothing queued at all when the store has neither a shipping nor a
    returns policy page reachable - there's nothing to compare a claim
    against, and this must not manufacture a CANNOT_VERIFY placeholder.
    Tests the internal task-builder directly (not run_llm_checks, which
    constructs its own real LLM client internally regardless of any fake
    client a test constructs - a fake response list here wouldn't actually
    be used, and a "fake-key-for-tests" Settings would attempt a real,
    failing network call for the homepage's own editorial-quality check)."""
    from app.llm.checks import _claim_contradiction_tasks

    homepage = _page("https://x.example/", PageType.HOMEPAGE, "Free shipping on all orders! Shop now.")
    site_map = SiteMap(base_url="https://x.example/", pages=[homepage])
    client = FakeClaudeClient([])
    tasks = await _claim_contradiction_tasks(client, site_map, _SETTINGS, None, None)
    assert tasks == []


@pytest.mark.asyncio
async def test_claim_contradiction_tasks_empty_when_page_has_no_matching_claim():
    """The pre-filter must not queue every homepage/product page for the
    LLM - only ones that actually mention a shipping/returns-adjacent claim."""
    from app.llm.checks import _claim_contradiction_tasks

    homepage = _page("https://x.example/", PageType.HOMEPAGE, "Welcome to our store. Browse our catalog.")
    policy_page = _page("https://x.example/shipping-policy", PageType.SHIPPING_POLICY, "We ship within 3-5 business days.")
    site_map = SiteMap(base_url="https://x.example/", pages=[homepage, policy_page])
    client = FakeClaudeClient([])
    tasks = await _claim_contradiction_tasks(client, site_map, _SETTINGS, None, None)
    assert tasks == []


@pytest.mark.asyncio
async def test_claim_contradiction_tasks_queued_when_claim_and_policy_both_present():
    from app.llm.checks import _claim_contradiction_tasks

    homepage = _page("https://x.example/", PageType.HOMEPAGE, "Free shipping on all orders! Shop now.")
    policy_page = _page("https://x.example/shipping-policy", PageType.SHIPPING_POLICY, "We ship within 3-5 business days.")
    site_map = SiteMap(base_url="https://x.example/", pages=[homepage, policy_page])
    client = FakeClaudeClient([])
    tasks = await _claim_contradiction_tasks(client, site_map, _SETTINGS, None, None)
    assert len(tasks) == 1
    for t in tasks:
        t.close()  # never awaited by design (this test only checks how many were queued) - avoid a ResourceWarning


@pytest.mark.asyncio
async def test_run_llm_checks_produces_cannot_verify_findings_when_no_api_key():
    settings = Settings(llm_provider="claude", anthropic_api_key="")
    page = _page("https://x.example/privacy-policy", PageType.PRIVACY_POLICY, "Our privacy practices...")
    site_map = SiteMap(base_url="https://x.example/", pages=[page])
    findings, coverage = await run_llm_checks(site_map, settings)
    assert len(findings) == 1
    assert findings[0].confidence == Confidence.CANNOT_VERIFY
    assert "not configured" in findings[0].evidence
    assert coverage.llm_configured is False
    assert coverage.product_pages_checked == 0


# --- LLM coverage stats (fixed-sample-size honesty fix) --------------------

@pytest.mark.asyncio
async def test_llm_coverage_reflects_sampled_vs_total_product_pages(monkeypatch):
    from app.llm.checks import _MAX_PRODUCT_PAGES_CHECKED

    pages = [_page(f"https://x.example/product/{i}", PageType.PRODUCT, f"Product {i} description.") for i in range(12)]
    site_map = SiteMap(base_url="https://x.example/", pages=pages)

    # A fake client that always says "no issue" for every call, so this
    # test only exercises the sampling/coverage bookkeeping, not grading.
    responses = []
    for _ in range(_MAX_PRODUCT_PAGES_CHECKED):
        responses.append({"has_quality_issue": False, "confidence": "confirmed", "evidence_quote": "", "location": "", "issue_description": "", "recommended_fix": ""})
        responses.append({"potentially_prohibited": False, "confidence": "confirmed", "matched_category": "", "evidence_quote": "", "location": "", "reasoning": ""})
    client = FakeClaudeClient(responses)
    monkeypatch.setattr("app.llm.checks.get_llm_client", lambda settings, cache: client)

    findings, coverage = await run_llm_checks(site_map, _SETTINGS)

    assert coverage.llm_configured is True
    assert coverage.total_reachable_product_pages == 12
    assert coverage.product_pages_checked == _MAX_PRODUCT_PAGES_CHECKED
    assert coverage.is_partial is True
    assert coverage.coverage_fraction == pytest.approx(_MAX_PRODUCT_PAGES_CHECKED / 12)


@pytest.mark.asyncio
async def test_llm_coverage_not_partial_when_catalog_smaller_than_sample(monkeypatch):
    from app.llm.checks import _MAX_PRODUCT_PAGES_CHECKED

    pages = [_page("https://x.example/product/1", PageType.PRODUCT, "Only product.")]
    site_map = SiteMap(base_url="https://x.example/", pages=pages)
    responses = [
        {"has_quality_issue": False, "confidence": "confirmed", "evidence_quote": "", "location": "", "issue_description": "", "recommended_fix": ""},
        {"potentially_prohibited": False, "confidence": "confirmed", "matched_category": "", "evidence_quote": "", "location": "", "reasoning": ""},
    ]
    client = FakeClaudeClient(responses)
    monkeypatch.setattr("app.llm.checks.get_llm_client", lambda settings, cache: client)

    findings, coverage = await run_llm_checks(site_map, _SETTINGS)

    assert coverage.total_reachable_product_pages == 1
    assert coverage.product_pages_checked == 1
    assert coverage.is_partial is False
    assert coverage.coverage_fraction == 1.0
    assert _MAX_PRODUCT_PAGES_CHECKED >= 1  # sanity: sample cap didn't limit this small catalog


# --- Adaptive risk-weighted product sampling (Part 1.3, option C) ----------

def test_product_sample_size_scales_with_catalog_up_to_cap():
    assert _product_sample_size(12, cap=15) == 5   # 5% of 12 rounds to 1, floor is 5
    assert _product_sample_size(100, cap=15) == 5  # 5% of 100 is 5, floor is 5
    assert _product_sample_size(214, cap=15) == 11  # 5% of 214 rounds up to 11 - above the floor, under the cap
    assert _product_sample_size(400, cap=15) == 15  # 5% of 400 is 20 -> capped at 15
    assert _product_sample_size(500, cap=15) == 15  # cap holds regardless of catalog size
    assert _product_sample_size(500, cap=50) == 25  # a deployment that raises the cap gets more coverage


def test_select_product_sample_prioritizes_cheap_and_thin_pages():
    # 20 "normal" products (mid-priced, substantial copy) crawled first, then
    # one suspiciously-cheap-for-this-store product and one near-empty
    # product page crawled last - risk-weighting should still surface both
    # despite crawl order, not just the first N found.
    long_description = " ".join(["This is a genuinely substantial product description with real detail about materials, sizing, and care instructions."] * 4)
    normal_pages = [
        _page(f"https://x.example/product/{i}", PageType.PRODUCT, f"$49.99 - {long_description} (item {i})")
        for i in range(20)
    ]
    cheap_outlier = _page("https://x.example/product/cheap", PageType.PRODUCT, "$0.99 - great deal on this item, buy now.")
    thin_page = _page("https://x.example/product/thin", PageType.PRODUCT, "In stock.")
    pages = normal_pages + [cheap_outlier, thin_page]

    sample = _select_product_sample(pages, cap=5)

    assert len(sample) == 5
    sample_urls = {p.url for p in sample}
    assert cheap_outlier.url in sample_urls
    assert thin_page.url in sample_urls


def test_select_product_sample_returns_everything_under_the_cap():
    pages = [_page(f"https://x.example/product/{i}", PageType.PRODUCT, "A product.") for i in range(3)]
    sample = _select_product_sample(pages, cap=15)
    assert sample == pages


@pytest.mark.asyncio
async def test_run_llm_checks_scales_sample_for_large_catalog(monkeypatch):
    settings = Settings(llm_provider="claude", anthropic_api_key="fake-key-for-tests", llm_product_sample_cap=15)
    pages = [_page(f"https://x.example/product/{i}", PageType.PRODUCT, f"$29.99 - product {i} with a full real description.") for i in range(400)]
    site_map = SiteMap(base_url="https://x.example/", pages=pages)

    expected_sample_size = _product_sample_size(400, cap=15)
    responses = []
    for _ in range(expected_sample_size):
        responses.append({"has_quality_issue": False, "confidence": "confirmed", "evidence_quote": "", "location": "", "issue_description": "", "recommended_fix": ""})
        responses.append({"potentially_prohibited": False, "confidence": "confirmed", "matched_category": "", "evidence_quote": "", "location": "", "reasoning": ""})
    client = FakeClaudeClient(responses)
    monkeypatch.setattr("app.llm.checks.get_llm_client", lambda settings, cache: client)

    findings, coverage = await run_llm_checks(site_map, settings)

    assert coverage.total_reachable_product_pages == 400
    assert coverage.product_pages_checked == expected_sample_size == 15
    assert coverage.coverage_fraction == pytest.approx(15 / 400)
