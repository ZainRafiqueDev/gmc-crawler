"""Follow-up round, Part 1: a required-page candidate the soft-404 check
(app/soft_404_detection.py) has flagged - its content strongly matches the
audit's known-nonexistent-URL baseline or the homepage - must not also get
an independent LLM-graded "substance" verdict presented as a second,
seemingly-corroborating finding in the same report. Observed live on
leafloop.site: the soft-404 CANNOT_VERIFY finding and a separate
"Privacy policy page lacks required substance" finding both appeared for
the exact same ambiguous /lander page.

Reuses app.soft_404_detection.soft_404_flagged_page_urls as the single
shared source of truth - see tests/test_soft_404_detection.py for that
function's own behavior; these tests only check that app/llm/checks.py
actually consults it.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.llm.checks import _claim_contradiction_tasks, run_llm_checks
from app.models import Confidence, CrawledPage, PageType, SiteMap

HOMEPAGE_TEXT = "Acme Gadgets Welcome to our store, home of quality widgets since 2010."
CATCH_ALL_TEXT = "Oops We couldn't find that page. Browse our homepage instead."
GENUINE_PRIVACY_TEXT = "Privacy Policy We collect your name, email, and shipping address when you place an order, and never sell this data to third parties."

_SETTINGS = Settings(llm_provider="claude", anthropic_api_key="fake-key-for-tests")


class _FakeClaudeClient:
    """Dispatches by tool_name (submit_policy_verdict / submit_editorial_verdict
    / ...) rather than a single shared FIFO queue, and tracks every call
    directly (call_count) rather than relying on an empty/exhausted response
    list raising an error.

    Both matter for the same reason: run_llm_checks queues several tasks
    (e.g. the homepage's own check_editorial_quality alongside a candidate
    page's check_policy_page_substance) and runs them concurrently via
    asyncio.gather(..., return_exceptions=True) - a shared FIFO queue would
    make which canned response goes to which call depend on non-
    deterministic task-scheduling order (a real, confirmed flakiness risk,
    not hypothetical), and gather's return_exceptions=True would silently
    swallow a wrong-call IndexError/crash, making "no finding produced" look
    identical to "properly skipped, never called" - exactly the distinction
    these tests need to make reliably.
    """

    def __init__(self, responses_by_tool: dict[str, dict] | None = None):
        self._responses_by_tool = dict(responses_by_tool or {})
        self.call_count = 0
        self.calls: list[str] = []

    async def call_tool(self, system: str, user: str, tool_name: str, tool_schema: dict, max_tokens: int = 1024):
        self.call_count += 1
        self.calls.append(tool_name)
        return self._responses_by_tool[tool_name]


def _page(url: str, page_type: PageType, text: str) -> CrawledPage:
    return CrawledPage(url=url, page_type=page_type, depth=1, reachable=True, text=text, html=f"<html><body>{text}</body></html>")


def _homepage() -> CrawledPage:
    return _page("https://acme.example/", PageType.HOMEPAGE, HOMEPAGE_TEXT)


@pytest.mark.asyncio
async def test_soft_404_flagged_page_gets_no_llm_substance_call(monkeypatch):
    """The candidate's content is literally the homepage's content (a
    homepage-redirect/catch-all shape) - soft_404_flagged_page_urls flags
    it, so no LLM substance call should ever be attempted for it. The
    homepage itself still legitimately gets its own, unrelated
    check_editorial_quality call (run_llm_checks always runs that on the
    homepage regardless of soft-404 detection) - accounted for below via
    call_count, not avoided, so this test can't pass by accident the way an
    empty-response-list design silently would (see _FakeClaudeClient's
    docstring)."""
    client = _FakeClaudeClient({
        "submit_editorial_verdict": {"has_quality_issue": False, "confidence": "confirmed", "evidence_quote": "", "location": "", "issue_description": "", "recommended_fix": ""},
    })
    monkeypatch.setattr("app.llm.checks.get_llm_client", lambda settings, cache: client)

    site_map = SiteMap(
        base_url="https://acme.example/",
        pages=[_homepage(), _page("https://acme.example/privacy-policy", PageType.PRIVACY_POLICY, HOMEPAGE_TEXT)],
    )
    findings, _coverage = await run_llm_checks(site_map, _SETTINGS)

    assert client.call_count == 1  # only the homepage's own (unrelated) editorial-quality check ran
    assert client.calls == ["submit_editorial_verdict"]  # submit_policy_verdict never called
    assert not any(f.check_id == "llm_policy_substance_privacy_policy" for f in findings)


@pytest.mark.asyncio
async def test_soft_404_flagged_page_gets_no_placeholder_when_llm_not_configured():
    settings = Settings(llm_provider="claude", anthropic_api_key="")
    site_map = SiteMap(
        base_url="https://acme.example/",
        pages=[_homepage(), _page("https://acme.example/privacy-policy", PageType.PRIVACY_POLICY, HOMEPAGE_TEXT)],
    )
    findings, _coverage = await run_llm_checks(site_map, settings)

    assert not any(f.check_id == "llm_policy_substance_privacy_policy" for f in findings)


@pytest.mark.asyncio
async def test_genuine_required_page_still_gets_graded_normally(monkeypatch):
    """Regression guard: the fix must not suppress grading for a page that
    ISN'T soft-404-flagged - a genuinely distinct privacy policy still gets
    its normal LLM substance check, alongside the homepage's own always-run
    editorial-quality check (both queued below, both expected to run)."""
    client = _FakeClaudeClient({
        "submit_editorial_verdict": {"has_quality_issue": False, "confidence": "confirmed", "evidence_quote": "", "location": "", "issue_description": "", "recommended_fix": ""},
        "submit_policy_verdict": {"meets_requirement": True, "confidence": "confirmed", "evidence_quote": "", "location": "", "reasoning": "Covers data collection and use.", "recommended_fix": ""},
    })
    monkeypatch.setattr("app.llm.checks.get_llm_client", lambda settings, cache: client)

    site_map = SiteMap(
        base_url="https://acme.example/",
        pages=[_homepage(), _page("https://acme.example/privacy-policy", PageType.PRIVACY_POLICY, GENUINE_PRIVACY_TEXT)],
    )
    findings, _coverage = await run_llm_checks(site_map, _SETTINGS)
    assert client.call_count == 2  # homepage editorial-quality + the privacy substance check, both genuinely ran
    assert set(client.calls) == {"submit_editorial_verdict", "submit_policy_verdict"}
    assert not any(f.check_id == "llm_policy_substance_privacy_policy" for f in findings)


@pytest.mark.asyncio
async def test_claim_contradiction_excludes_a_soft_404_flagged_policy_page():
    """A shipping-policy candidate whose content is really the homepage
    must not be used as the comparison target for claim-vs-policy
    contradiction checking - grounding a "contradiction" verdict on content
    that was never confirmed to be the real policy page would be worse than
    not checking at all."""
    client = _FakeClaudeClient({})
    claim_page = _page("https://acme.example/product/widget", PageType.PRODUCT, "Free shipping on every order! Buy now.")
    flagged_shipping_page = _page("https://acme.example/shipping-policy", PageType.SHIPPING_POLICY, HOMEPAGE_TEXT)
    site_map = SiteMap(base_url="https://acme.example/", pages=[_homepage(), claim_page, flagged_shipping_page])

    tasks = await _claim_contradiction_tasks(client, site_map, _SETTINGS, None, None)

    assert tasks == []


@pytest.mark.asyncio
async def test_claim_contradiction_still_runs_against_a_genuine_policy_page():
    client = _FakeClaudeClient({
        "submit_claim_contradiction_verdict": {
            "has_contradiction": False, "confidence": "confirmed", "conflict_dimension": "none",
            "claim_quote": "", "policy_quote": "", "location": "", "reasoning": "Consistent.", "recommended_fix": "",
        },
    })
    claim_page = _page("https://acme.example/product/widget", PageType.PRODUCT, "Free shipping on every order! Buy now.")
    genuine_shipping_page = _page("https://acme.example/shipping-policy", PageType.SHIPPING_POLICY, "We charge a flat $5 shipping fee on all orders.")
    site_map = SiteMap(base_url="https://acme.example/", pages=[_homepage(), claim_page, genuine_shipping_page])

    tasks = await _claim_contradiction_tasks(client, site_map, _SETTINGS, None, None)

    assert len(tasks) == 1
    for t in tasks:
        t.close()
