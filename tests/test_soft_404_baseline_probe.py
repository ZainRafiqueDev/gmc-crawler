"""Unit tests for app.site_mapper._probe_soft_404_baseline - the once-per-
audit fetch of a guaranteed-nonexistent URL that establishes the known-
nonexistent content signature app.checks.deterministic.check_required_pages
compares candidates against (see tests/test_soft_404_detection.py for the
check-level behavior).
"""
from __future__ import annotations

import pytest

from app.change_detection import compute_content_hash, normalize_for_content_hash
from app.fetch import FetchResult
from app.site_mapper import _probe_soft_404_baseline


class _FakeFetcher:
    def __init__(self, result: FetchResult):
        self._result = result
        self.requested_url: str | None = None

    async def fetch(self, url: str) -> FetchResult:
        self.requested_url = url
        return self._result


@pytest.mark.asyncio
async def test_successful_probe_returns_hash_and_normalized_text():
    result = FetchResult(url="x", ok=True, status=200, text="Page not found", html="<html>...</html>", final_url="https://acme.example/__gmc_nonexistent_abc")
    fetcher = _FakeFetcher(result)

    content_hash, normalized_text = await _probe_soft_404_baseline(fetcher, "https://acme.example/")

    assert content_hash == compute_content_hash("Page not found")
    assert normalized_text == normalize_for_content_hash("Page not found")


@pytest.mark.asyncio
async def test_probe_uses_an_audit_scoped_token_url_under_the_homepage():
    result = FetchResult(url="x", ok=True, status=200, text="anything")
    fetcher = _FakeFetcher(result)

    await _probe_soft_404_baseline(fetcher, "https://acme.example/")

    assert fetcher.requested_url.startswith("https://acme.example/__gmc_nonexistent_")
    # Not a fixed, hardcoded path re-used across audits/runs.
    assert fetcher.requested_url != "https://acme.example/__gmc_nonexistent_"


@pytest.mark.asyncio
async def test_two_probes_use_different_tokens():
    fetcher_a = _FakeFetcher(FetchResult(url="x", ok=True, status=200, text="a"))
    fetcher_b = _FakeFetcher(FetchResult(url="x", ok=True, status=200, text="b"))

    await _probe_soft_404_baseline(fetcher_a, "https://acme.example/")
    await _probe_soft_404_baseline(fetcher_b, "https://acme.example/")

    assert fetcher_a.requested_url != fetcher_b.requested_url


@pytest.mark.asyncio
async def test_failed_probe_degrades_to_no_baseline_without_raising():
    result = FetchResult(url="x", ok=False, status=None, network_error=True, error="DNS failure")
    fetcher = _FakeFetcher(result)

    content_hash, normalized_text = await _probe_soft_404_baseline(fetcher, "https://acme.example/")

    assert content_hash is None
    assert normalized_text is None
