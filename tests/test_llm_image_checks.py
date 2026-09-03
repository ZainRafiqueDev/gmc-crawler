from unittest.mock import patch

import pytest

from app.checks.product_images import ProductImage
from app.config import Settings
from app.llm.image_checks import check_image_with_vision, run_llm_image_checks
from app.models import Confidence, CrawledPage, PageType, Severity, SiteMap, VerificationMethod


class FakeClaudeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def call_tool_with_image(self, system, user_text, image_url, tool_name, tool_schema, max_tokens=1024):
        self.calls.append((system, user_text, image_url, tool_name))
        return self._responses.pop(0)


def _page(url, title="Widget", text="A red widget."):
    return CrawledPage(url=url, page_type=PageType.PRODUCT, depth=1, reachable=True, title=title, text=text, html=f"<html><body>{text}</body></html>")


def _img(url="https://shop.example/img/widget.jpg"):
    return ProductImage(url=url, alt_text="widget", source=VerificationMethod.API_VERIFIED)


@pytest.mark.asyncio
async def test_no_findings_when_image_matches_and_clean():
    client = FakeClaudeClient([{
        "plausible_match": True, "match_confidence": "confirmed", "mismatch_reasoning": "",
        "potentially_prohibited": False, "prohibited_category": "", "prohibited_reasoning": "",
    }])
    findings = await check_image_with_vision(client, _page("https://shop.example/products/widget"), _img())
    assert findings == []


@pytest.mark.asyncio
async def test_flags_mismatch_with_real_reasoning():
    client = FakeClaudeClient([{
        "plausible_match": False, "match_confidence": "potential_risk",
        "mismatch_reasoning": "Image shows a lamp, page describes a widget.",
        "location": "main product photo",
        "potentially_prohibited": False, "prohibited_category": "", "prohibited_reasoning": "",
    }])
    findings = await check_image_with_vision(client, _page("https://shop.example/products/widget"), _img())
    assert len(findings) == 1
    assert findings[0].check_id == "llm_image_product_mismatch"
    assert findings[0].confidence == Confidence.POTENTIAL_RISK
    assert "lamp" in findings[0].evidence
    assert findings[0].location == "main product photo"
    assert findings[0].detected_at is not None


@pytest.mark.asyncio
async def test_prohibited_flag_is_always_potential_risk_never_confirmed():
    client = FakeClaudeClient([{
        "plausible_match": True, "match_confidence": "confirmed", "mismatch_reasoning": "",
        "potentially_prohibited": True, "prohibited_category": "weapons",
        "prohibited_reasoning": "Appears to show a knife.",
    }])
    findings = await check_image_with_vision(client, _page("https://shop.example/products/widget"), _img())
    assert len(findings) == 1
    f = findings[0]
    assert f.check_id == "llm_image_prohibited_content_flag"
    assert f.severity == Severity.HIGH
    assert f.confidence == Confidence.POTENTIAL_RISK  # never CONFIRMED, regardless of model's stated confidence
    assert "weapons" in f.title
    assert "human review" in f.recommended_fix.lower()


@pytest.mark.asyncio
async def test_both_mismatch_and_prohibited_can_fire_together():
    client = FakeClaudeClient([{
        "plausible_match": False, "match_confidence": "confirmed", "mismatch_reasoning": "wrong item",
        "potentially_prohibited": True, "prohibited_category": "dangerous products",
        "prohibited_reasoning": "looks concerning",
    }])
    findings = await check_image_with_vision(client, _page("https://shop.example/products/widget"), _img())
    assert len(findings) == 2
    assert {f.check_id for f in findings} == {"llm_image_product_mismatch", "llm_image_prohibited_content_flag"}


@pytest.mark.asyncio
async def test_cannot_verify_when_llm_call_fails():
    client = FakeClaudeClient([None])
    findings = await check_image_with_vision(client, _page("https://shop.example/products/widget"), _img())
    assert len(findings) == 1
    assert findings[0].confidence == Confidence.CANNOT_VERIFY


@pytest.mark.asyncio
async def test_run_llm_image_checks_no_api_key_produces_cannot_verify():
    page = _page("https://shop.example/products/widget")
    site_map = SiteMap(base_url="https://shop.example/", pages=[page])
    page_images = {page.url: [_img()]}
    settings = Settings(llm_provider="claude", anthropic_api_key="")

    findings = await run_llm_image_checks(site_map, page_images, settings)
    assert len(findings) == 1
    assert findings[0].confidence == Confidence.CANNOT_VERIFY


@pytest.mark.asyncio
async def test_run_llm_image_checks_skips_pages_with_no_images():
    page = _page("https://shop.example/products/widget")
    site_map = SiteMap(base_url="https://shop.example/", pages=[page])
    settings = Settings(llm_provider="claude", anthropic_api_key="")

    findings = await run_llm_image_checks(site_map, {page.url: []}, settings)
    assert findings == []


@pytest.mark.asyncio
async def test_run_llm_image_checks_caps_at_max_pages(monkeypatch):
    pages = [_page(f"https://shop.example/products/item{i}") for i in range(8)]
    site_map = SiteMap(base_url="https://shop.example/", pages=pages)
    page_images = {p.url: [_img(f"https://shop.example/img/item{i}.jpg")] for i, p in enumerate(pages)}
    settings = Settings(llm_provider="claude", anthropic_api_key="")

    findings = await run_llm_image_checks(site_map, page_images, settings)
    assert len(findings) == 5  # _MAX_PRODUCT_PAGES_CHECKED


@pytest.mark.asyncio
async def test_same_image_url_across_pages_graded_once_and_applied_to_both():
    page_a = _page("https://shop.example/products/bundle-a")
    page_b = _page("https://shop.example/products/bundle-b")
    shared_image = "https://shop.example/img/shared-hero.jpg"
    site_map = SiteMap(base_url="https://shop.example/", pages=[page_a, page_b])
    page_images = {page_a.url: [_img(shared_image)], page_b.url: [_img(shared_image)]}
    settings = Settings(llm_provider="claude", anthropic_api_key="sk-test")

    fake_client = FakeClaudeClient([{
        "plausible_match": False, "match_confidence": "confirmed",
        "mismatch_reasoning": "wrong item", "location": "main photo",
        "potentially_prohibited": False, "prohibited_category": "", "prohibited_reasoning": "",
    }])

    with patch("app.llm.image_checks.get_llm_client", return_value=fake_client):
        findings = await run_llm_image_checks(site_map, page_images, settings)

    assert len(fake_client.calls) == 1  # graded once despite appearing on 2 pages
    assert len(findings) == 2  # but the result was applied to both pages
    assert {f.page_url for f in findings} == {page_a.url, page_b.url}
