"""Adaptive page-budget scaling (follow-up round, Part 1.1). _adaptive_page_budget
is a pure function - covered directly. The wiring test below (real signal ->
settings.crawl_max_pages actually changing, explicit override winning) uses a
fully mocked crawl (no real network/browser) since app.site_mapper._map_site
otherwise needs a live Playwright Browser - live end-to-end confirmation
against real stores is done separately, not substituted by this test.
"""
from __future__ import annotations

import pytest

from app.config import HARD_MAX_PAGES, Settings
from app.fetch import FetchResult
from app.models import Platform
from app.site_mapper import _adaptive_page_budget, _map_site


# --- _adaptive_page_budget (pure formula) -----------------------------------

def test_no_signal_keeps_configured_default():
    assert _adaptive_page_budget(catalog_url_count=0, total_sitemap_url_count=0, configured_default=150) == 150


def test_small_catalog_signal_scales_down_but_keeps_a_floor():
    # 5 catalog URLs -> 5*2 + 40 = 50, below the 60 floor.
    assert _adaptive_page_budget(catalog_url_count=5, total_sitemap_url_count=5, configured_default=150) == 60


def test_large_catalog_signal_scales_up():
    # 500 catalog URLs -> 500*2 + 40 = 1040, no cap applied here (the
    # Settings field_validator is what enforces HARD_MAX_PAGES on assignment).
    assert _adaptive_page_budget(catalog_url_count=500, total_sitemap_url_count=520, configured_default=150) == 1040


def test_falls_back_to_total_sitemap_count_when_no_catalog_urls_tagged():
    assert _adaptive_page_budget(catalog_url_count=0, total_sitemap_url_count=90, configured_default=150) == 90


def test_settings_clamps_an_oversized_adaptive_value_to_the_hard_ceiling():
    settings = Settings()
    settings.crawl_max_pages = _adaptive_page_budget(catalog_url_count=500, total_sitemap_url_count=520, configured_default=150)
    assert settings.crawl_max_pages == HARD_MAX_PAGES


# --- Wiring: real signal actually resizes settings.crawl_max_pages ---------

class _FakeRobots:
    def __init__(self, base_url: str) -> None:
        pass

    async def load(self) -> None:
        pass

    def is_allowed(self, url: str) -> bool:
        return True


class _FakeFetcher:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def fetch(self, url: str) -> FetchResult:
        return FetchResult(url=url, ok=True, status=200, html="<html><body><h1>Home</h1></body></html>", text="Home", final_url=url, ssrf_requests_validated=1)


@pytest.mark.asyncio
async def test_map_site_scales_budget_from_sitemap_catalog_signal(monkeypatch):
    catalog_urls = [f"https://shop.example/product/{i}" for i in range(80)]
    monkeypatch.setattr("app.site_mapper._fetch_sitemap_urls", lambda base_url: _async_return(catalog_urls))
    monkeypatch.setattr("app.site_mapper.RobotsChecker", _FakeRobots)
    monkeypatch.setattr("app.site_mapper.PageFetcher", _FakeFetcher)
    monkeypatch.setattr("app.page_classifier.looks_like_catalog_priority_url", lambda path: True)
    monkeypatch.setattr("app.site_mapper.looks_like_catalog_priority_url", lambda path: True)

    settings = Settings(crawl_max_pages=150)
    assert settings.crawl_max_pages_explicit is False

    site_map = await _map_site("https://shop.example", browser=None, settings=settings, proxy_rotator=None)

    # 80 catalog URLs -> 80*2 + 40 = 200, well above the flat 150 default.
    assert settings.crawl_max_pages == 200
    assert site_map is not None


@pytest.mark.asyncio
async def test_map_site_does_not_resize_when_caller_set_an_explicit_override(monkeypatch):
    catalog_urls = [f"https://shop.example/product/{i}" for i in range(80)]
    monkeypatch.setattr("app.site_mapper._fetch_sitemap_urls", lambda base_url: _async_return(catalog_urls))
    monkeypatch.setattr("app.site_mapper.RobotsChecker", _FakeRobots)
    monkeypatch.setattr("app.site_mapper.PageFetcher", _FakeFetcher)
    monkeypatch.setattr("app.site_mapper.looks_like_catalog_priority_url", lambda path: True)

    settings = Settings(crawl_max_pages=150)
    settings.crawl_max_pages_explicit = True

    await _map_site("https://shop.example", browser=None, settings=settings, proxy_rotator=None)

    assert settings.crawl_max_pages == 150  # untouched despite a real, large catalog signal


@pytest.mark.asyncio
async def test_map_site_prefers_woocommerce_product_count_over_sitemap_signal(monkeypatch):
    catalog_urls = [f"https://shop.example/product/{i}" for i in range(80)]  # would size to 200
    monkeypatch.setattr("app.site_mapper._fetch_sitemap_urls", lambda base_url: _async_return(catalog_urls))
    monkeypatch.setattr("app.site_mapper.fetch_wc_product_count", lambda *a, **k: _async_return(1000))  # sizes to 2040
    monkeypatch.setattr("app.site_mapper.RobotsChecker", _FakeRobots)
    monkeypatch.setattr("app.site_mapper.PageFetcher", _FakeFetcher)
    monkeypatch.setattr("app.site_mapper.looks_like_catalog_priority_url", lambda path: True)

    settings = Settings(crawl_max_pages=150, wc_consumer_key="k", wc_consumer_secret="s")

    await _map_site("https://shop.example", browser=None, settings=settings, proxy_rotator=None, platform=Platform.WOOCOMMERCE)

    assert settings.crawl_max_pages == HARD_MAX_PAGES  # 1000*2+40=2040, clamped to 500


async def _async_return(value):
    return value
