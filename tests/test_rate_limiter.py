"""Section 4.2: confirm rate limits actually trigger under a simulated
burst of requests, per-key isolation holds, and the window actually slides.
"""
import asyncio

import pytest

from app.security.rate_limiter import RateLimiter


@pytest.mark.asyncio
async def test_allows_up_to_the_limit_then_blocks():
    limiter = RateLimiter(max_requests=3, window_seconds=60)
    results = [await limiter.allow("1.2.3.4") for _ in range(5)]
    assert results == [True, True, True, False, False]


@pytest.mark.asyncio
async def test_simulated_burst_of_concurrent_requests_only_allows_the_limit():
    limiter = RateLimiter(max_requests=5, window_seconds=60)
    results = await asyncio.gather(*(limiter.allow("burst-key") for _ in range(20)))
    assert sum(results) == 5
    assert sum(1 for r in results if not r) == 15


@pytest.mark.asyncio
async def test_different_keys_are_independent():
    limiter = RateLimiter(max_requests=1, window_seconds=60)
    assert await limiter.allow("ip-a") is True
    assert await limiter.allow("ip-a") is False
    assert await limiter.allow("ip-b") is True  # unaffected by ip-a's limit


@pytest.mark.asyncio
async def test_window_slides_and_allows_again_after_expiry(monkeypatch):
    fake_time = {"now": 1000.0}
    monkeypatch.setattr("time.monotonic", lambda: fake_time["now"])

    limiter = RateLimiter(max_requests=1, window_seconds=10)
    assert await limiter.allow("key") is True
    assert await limiter.allow("key") is False

    fake_time["now"] += 11  # past the window
    assert await limiter.allow("key") is True


@pytest.mark.asyncio
async def test_remaining_reports_correctly():
    limiter = RateLimiter(max_requests=3, window_seconds=60)
    assert await limiter.remaining("key") == 3
    await limiter.allow("key")
    assert await limiter.remaining("key") == 2
    await limiter.allow("key")
    await limiter.allow("key")
    assert await limiter.remaining("key") == 0


@pytest.mark.asyncio
async def test_reset_clears_a_key():
    limiter = RateLimiter(max_requests=1, window_seconds=60)
    await limiter.allow("key")
    assert await limiter.allow("key") is False
    await limiter.reset("key")
    assert await limiter.allow("key") is True
