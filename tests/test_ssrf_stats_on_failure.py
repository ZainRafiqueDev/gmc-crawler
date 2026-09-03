"""Follow-up round, Part 2: a real report on a genuine 20-second Playwright
navigation timeout showed "0 request(s) validated" in the SSRF guard stat -
misleading, since the navigation request legitimately passed
assert_public_url before the timeout occurred; the guard checked it, it just
never got a completed round trip. Two distinct bugs compounded to produce
this: (1) app.security.ssrf_guard counted "validated" only after a full
round trip (route.fetch()+fulfill()) rather than the moment validation
passed - fixed and covered in tests/test_ssrf_guard.py; (2)
app.site_mapper._fetch_and_classify silently dropped FetchResult's
ssrf_requests_validated/blocked entirely on the failure path when building
the CrawledPage - fixed here.
"""
import asyncio

import pytest

from app.models import PageType
from app.site_mapper import _fetch_and_classify


class _FakeFetcher:
    """Stands in for app.fetch.PageFetcher - returns a pre-built FetchResult
    without touching a real browser, so this test only exercises
    _fetch_and_classify's own CrawledPage-building logic."""

    def __init__(self, result):
        self._result = result

    async def fetch(self, url):
        return self._result


@pytest.mark.asyncio
async def test_failed_fetch_still_carries_nonzero_ssrf_stats_onto_the_crawled_page():
    from app.fetch import FetchResult

    result = FetchResult(
        url="https://shop.example/", ok=False, attempts=3,
        error="Page.goto: Timeout 20000ms exceeded.", network_error=True,
        # The navigation request legitimately passed the SSRF guard 3 times
        # (once per attempt) before each attempt's connection then timed out -
        # this must not collapse to 0 on the CrawledPage.
        ssrf_requests_validated=3, ssrf_requests_blocked=0,
    )
    fetcher = _FakeFetcher(result)

    page = await _fetch_and_classify(fetcher, "https://shop.example/", depth=0, home_netloc="shop.example", is_homepage=True)

    assert page.reachable is False
    assert page.cannot_verify is True
    assert page.ssrf_requests_validated == 3
    assert page.ssrf_requests_blocked == 0


@pytest.mark.asyncio
async def test_successful_fetch_still_carries_ssrf_stats_as_before():
    from app.fetch import FetchResult

    result = FetchResult(
        url="https://shop.example/", ok=True, status=200, html="<html><body>ok</body></html>", text="ok",
        final_url="https://shop.example/", attempts=1,
        ssrf_requests_validated=5, ssrf_requests_blocked=1,
    )
    fetcher = _FakeFetcher(result)

    page = await _fetch_and_classify(fetcher, "https://shop.example/", depth=0, home_netloc="shop.example", is_homepage=True)

    assert page.reachable is True
    assert page.ssrf_requests_validated == 5
    assert page.ssrf_requests_blocked == 1
