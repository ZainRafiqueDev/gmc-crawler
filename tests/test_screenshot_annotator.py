"""Annotated screenshots for Suspension Risk Findings (follow-up round,
Part 3). Playwright browser/context/page mocking follows the same pattern
already established in tests/test_fetch.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from app.checks.screenshot_annotator import (
    _candidate_quotes,
    _is_screenshot_eligible,
    _safe_slug,
    capture_annotated_screenshots,
)
from app.config import Settings
from app.models import Confidence, Finding, ImpactTier, Severity


def _finding(**overrides) -> Finding:
    defaults = dict(
        check_id="llm_prohibited_content", title="Potentially prohibited content", severity=Severity.CRITICAL,
        confidence=Confidence.CONFIRMED, page_url="https://shop.example/products/widget",
        evidence="AAA quality 1:1 mirror replica", impact_tier=ImpactTier.SUSPENSION_RISK,
    )
    defaults.update(overrides)
    return Finding(**defaults)


# --- _is_screenshot_eligible -------------------------------------------

def test_llm_suspension_risk_finding_with_quote_and_page_is_eligible():
    assert _is_screenshot_eligible(_finding()) is True


def test_deterministic_aggregate_finding_is_never_eligible():
    """business_identity_email_consistency is SUSPENSION_RISK-graded but
    site-wide/aggregate and deterministic (no model-verified quote to
    anchor on) - scope already confirmed to skip these entirely."""
    f = _finding(check_id="business_identity_email_consistency", impact_tier=ImpactTier.SUSPENSION_RISK)
    assert _is_screenshot_eligible(f) is False


def test_non_suspension_risk_llm_finding_is_not_eligible():
    f = _finding(check_id="llm_editorial_quality", impact_tier=ImpactTier.QUALITY_IMPROVEMENT, severity=Severity.LOW)
    assert _is_screenshot_eligible(f) is False


def test_vision_check_is_explicitly_excluded_even_if_suspension_risk():
    f = _finding(check_id="llm_image_vision_check")
    assert _is_screenshot_eligible(f) is False


def test_cannot_verify_finding_is_not_eligible():
    f = _finding(confidence=Confidence.CANNOT_VERIFY)
    assert _is_screenshot_eligible(f) is False


def test_finding_with_no_page_url_is_not_eligible():
    f = _finding(page_url=None)
    assert _is_screenshot_eligible(f) is False


def test_finding_with_empty_evidence_is_not_eligible():
    f = _finding(evidence="")
    assert _is_screenshot_eligible(f) is False


# --- _candidate_quotes ---------------------------------------------------

def test_direct_quote_evidence_returns_itself():
    assert _candidate_quotes("AAA quality 1:1 mirror replica") == ["AAA quality 1:1 mirror replica"]


def test_composite_claim_contradiction_evidence_extracts_quoted_substrings_first():
    evidence = 'Claim on https://x/product: "30-Day Free Returns" — contradicts https://x/returns: "no returns after 14 days"'
    candidates = _candidate_quotes(evidence)
    assert candidates[0] == "30-Day Free Returns"
    assert candidates[1] == "no returns after 14 days"
    assert candidates[-1] == evidence  # whole string still tried as a last resort


# --- _safe_slug ------------------------------------------------------------

def test_safe_slug_strips_non_alnum():
    assert _safe_slug("/products/blue-mug?ref=1") == "products-blue-mug-ref-1"


def test_safe_slug_never_empty():
    assert _safe_slug("///") == "finding"


# --- capture_annotated_screenshots (integration, mocked Playwright) -------

def _png_bytes(width=100, height=80) -> bytes:
    import io
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color="blue").save(buf, format="PNG")
    return buf.getvalue()


def _make_browser(evaluate_return, screenshot_bytes=None):
    context = MagicMock()
    context.add_init_script = AsyncMock()
    context.close = AsyncMock()
    page = MagicMock()
    page.goto = AsyncMock()
    page.evaluate = AsyncMock(return_value=evaluate_return)
    page.screenshot = AsyncMock(return_value=screenshot_bytes or _png_bytes())
    page.viewport_size = {"width": 1366, "height": 768}
    context.new_page = AsyncMock(return_value=page)
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    return browser, context, page


@pytest.fixture(autouse=True)
def _no_real_ssrf_guard(monkeypatch):
    from app.security.ssrf_guard import SSRFGuardStats

    async def fake_install_ssrf_guard(context):
        return SSRFGuardStats(validated=1, blocked=0)

    monkeypatch.setattr("app.checks.screenshot_annotator.install_ssrf_guard", fake_install_ssrf_guard)


@pytest.mark.asyncio
async def test_eligible_finding_gets_screenshot_path_when_quote_is_located(tmp_path):
    box = {"x": 10, "y": 10, "width": 100, "height": 40}
    browser, context, page = _make_browser(evaluate_return=box)
    findings = [_finding()]

    result = await capture_annotated_screenshots(browser, findings, Settings(), tmp_path, filename_prefix="shop-example")

    assert result[0].screenshot_path is not None
    assert result[0].screenshot_path.startswith("screenshots/shop-example-")
    saved = tmp_path / result[0].screenshot_path
    assert saved.is_file()
    page.goto.assert_awaited_once()


@pytest.mark.asyncio
async def test_quote_not_found_leaves_screenshot_path_none(tmp_path):
    browser, context, page = _make_browser(evaluate_return=None)
    findings = [_finding()]

    result = await capture_annotated_screenshots(browser, findings, Settings(), tmp_path, filename_prefix="shop-example")

    assert result[0].screenshot_path is None
    assert not (tmp_path / "screenshots").exists() or list((tmp_path / "screenshots").iterdir()) == []


@pytest.mark.asyncio
async def test_non_eligible_findings_pass_through_untouched(tmp_path):
    browser, context, page = _make_browser(evaluate_return={"x": 0, "y": 0, "width": 50, "height": 20})
    aggregate = _finding(check_id="business_identity_email_consistency", impact_tier=ImpactTier.SUSPENSION_RISK)

    result = await capture_annotated_screenshots(browser, [aggregate], Settings(), tmp_path, filename_prefix="shop-example")

    assert result == [aggregate]
    browser.new_context.assert_not_called()  # no eligible finding -> no visit at all


@pytest.mark.asyncio
async def test_multiple_findings_on_same_page_share_one_visit(tmp_path):
    box = {"x": 0, "y": 0, "width": 50, "height": 20}
    browser, context, page = _make_browser(evaluate_return=box)
    findings = [_finding(check_id="llm_prohibited_content"), _finding(check_id="llm_claim_policy_contradiction", evidence='"free shipping"')]

    result = await capture_annotated_screenshots(browser, findings, Settings(), tmp_path, filename_prefix="shop-example")

    browser.new_context.assert_awaited_once()  # one page, one visit, regardless of finding count
    assert all(f.screenshot_path is not None for f in result)


@pytest.mark.asyncio
async def test_findings_on_different_pages_get_separate_visits(tmp_path):
    box = {"x": 0, "y": 0, "width": 50, "height": 20}
    browser, context, page = _make_browser(evaluate_return=box)
    findings = [
        _finding(page_url="https://shop.example/products/a"),
        _finding(page_url="https://shop.example/products/b"),
    ]

    await capture_annotated_screenshots(browser, findings, Settings(), tmp_path, filename_prefix="shop-example")

    assert browser.new_context.await_count == 2


@pytest.mark.asyncio
async def test_navigation_failure_skips_that_pages_findings_without_raising(tmp_path):
    context = MagicMock()
    context.add_init_script = AsyncMock()
    context.close = AsyncMock()
    page = MagicMock()
    page.goto = AsyncMock(side_effect=RuntimeError("navigation timeout"))
    context.new_page = AsyncMock(return_value=page)
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)

    result = await capture_annotated_screenshots(browser, [_finding()], Settings(), tmp_path, filename_prefix="shop-example")

    assert result[0].screenshot_path is None
    context.close.assert_awaited_once()  # context still cleaned up despite the failure
