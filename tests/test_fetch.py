"""Unit tests for the resilient fetch layer (app/fetch.py).

Playwright's Browser/Page objects are mocked so these run with no real
browser or network - they exercise the retry/backoff/classification logic
in isolation. Live-browser behavior is exercised manually against real URLs
(see project notes); that's not something a fast unit suite should depend on.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from app.fetch import PageFetcher
from app.security.ssrf_guard import DNSResolutionError, SSRFBlockedError


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    """The SSRF guard does real DNS resolution - these tests use fake
    domains (x.example) and must stay network-free, so neutralize it here.
    Dedicated SSRF tests below override this per-test to simulate a block.
    """
    async def fake_assert_public_url(url):
        return None
    monkeypatch.setattr("app.fetch.assert_public_url", fake_assert_public_url)


def _make_browser_with_pages(page_factory):
    """page_factory: callable(attempt_index) -> mock page object (or raises)."""
    browser = MagicMock()
    call_count = {"n": 0}

    async def new_context(**kwargs):
        context = MagicMock()

        async def new_page():
            idx = call_count["n"]
            call_count["n"] += 1
            return page_factory(idx)

        context.new_page = new_page
        context.close = AsyncMock()
        context.route = AsyncMock()
        context.add_init_script = AsyncMock()
        return context

    browser.new_context = new_context
    return browser


def _ok_page(status=200, html="<html><body>ok</body></html>", text="ok", url="https://x.example/"):
    page = MagicMock()
    response = MagicMock()
    response.status = status
    page.goto = AsyncMock(return_value=response)
    page.wait_for_load_state = AsyncMock()
    page.content = AsyncMock(return_value=html)
    page.inner_text = AsyncMock(return_value=text)
    page.url = url
    return page


def _timeout_page():
    page = MagicMock()
    page.goto = AsyncMock(side_effect=PlaywrightTimeoutError("Timeout 20000ms exceeded"))
    return page


def _status_page(status, html="<html><body>error page</body></html>"):
    page = MagicMock()
    response = MagicMock()
    response.status = status
    response.header_value = AsyncMock(return_value=None)
    page.goto = AsyncMock(return_value=response)
    page.wait_for_load_state = AsyncMock()
    page.content = AsyncMock(return_value=html)
    return page


@pytest.mark.asyncio
async def test_succeeds_on_first_attempt():
    browser = _make_browser_with_pages(lambda i: _ok_page())
    fetcher = PageFetcher(browser, max_attempts=3, base_backoff_seconds=0.01)
    result = await fetcher.fetch("https://x.example/")
    assert result.ok is True
    assert result.attempts == 1
    assert result.html == "<html><body>ok</body></html>"


@pytest.mark.asyncio
async def test_retries_on_timeout_then_succeeds():
    pages = [_timeout_page(), _timeout_page(), _ok_page()]
    browser = _make_browser_with_pages(lambda i: pages[i])
    fetcher = PageFetcher(browser, max_attempts=3, base_backoff_seconds=0.01)
    result = await fetcher.fetch("https://x.example/")
    assert result.ok is True
    assert result.attempts == 3


@pytest.mark.asyncio
async def test_marks_cannot_verify_only_after_all_retries_exhausted():
    browser = _make_browser_with_pages(lambda i: _timeout_page())
    fetcher = PageFetcher(browser, max_attempts=3, base_backoff_seconds=0.01)
    result = await fetcher.fetch("https://x.example/")
    assert result.ok is False
    assert result.cannot_verify is True
    assert result.attempts == 3


@pytest.mark.asyncio
async def test_404_is_confirmed_not_found_not_cannot_verify_and_does_not_retry():
    calls = {"n": 0}

    def factory(i):
        calls["n"] += 1
        return _status_page(404)

    browser = _make_browser_with_pages(factory)
    fetcher = PageFetcher(browser, max_attempts=3, base_backoff_seconds=0.01)
    result = await fetcher.fetch("https://x.example/missing")
    assert result.ok is False
    assert result.confirmed_not_found is True
    assert result.cannot_verify is False
    assert result.attempts == 1
    assert calls["n"] == 1  # no retries wasted on a confirmed 404


@pytest.mark.asyncio
async def test_5xx_retries_then_cannot_verify_if_never_recovers():
    browser = _make_browser_with_pages(lambda i: _status_page(503))
    fetcher = PageFetcher(browser, max_attempts=3, base_backoff_seconds=0.01)
    result = await fetcher.fetch("https://x.example/flaky")
    assert result.ok is False
    assert result.confirmed_not_found is False
    assert result.cannot_verify is True
    assert result.attempts == 3


@pytest.mark.asyncio
async def test_networkidle_timeout_falls_back_to_domcontentloaded_snapshot():
    page = _ok_page()
    page.wait_for_load_state = AsyncMock(side_effect=PlaywrightTimeoutError("networkidle never reached"))
    browser = _make_browser_with_pages(lambda i: page)
    fetcher = PageFetcher(browser, max_attempts=3, base_backoff_seconds=0.01)
    result = await fetcher.fetch("https://x.example/spa-with-polling")
    assert result.ok is True
    assert result.html == "<html><body>ok</body></html>"


@pytest.mark.asyncio
async def test_bodyless_response_retries_with_guard_still_installed_every_attempt():
    """A body-less response triggers a retry, but the guard is installed on
    every attempt unconditionally now - no toggle, no attempt skips it (see
    app/security/ssrf_guard.py for why route.fetch()+fulfill() means this
    shouldn't happen in practice anymore; kept as a retry trigger for
    genuine transient truncation unrelated to the guard).
    """
    bodyless = _ok_page(html="<html><head></head></html>")
    full = _ok_page(html="<html><body>real content</body></html>")
    pages = [bodyless, full]
    contexts: list[MagicMock] = []

    async def new_context(**kwargs):
        context = MagicMock()
        context.close = AsyncMock()
        context.route = AsyncMock()
        context.add_init_script = AsyncMock()
        idx = len(contexts)
        context.new_page = AsyncMock(return_value=pages[idx])
        contexts.append(context)
        return context

    browser = MagicMock()
    browser.new_context = new_context
    fetcher = PageFetcher(browser, max_attempts=3, base_backoff_seconds=0.01)

    result = await fetcher.fetch("https://x.example/")

    assert result.ok is True
    assert result.attempts == 2
    assert result.html == "<html><body>real content</body></html>"
    contexts[0].route.assert_awaited_once()  # guard installed on the failed attempt
    contexts[1].route.assert_awaited_once()  # AND on the retry - never skipped


@pytest.mark.asyncio
async def test_final_url_check_blocks_a_malicious_redirect(monkeypatch):
    """The per-request guard validates each request's own URL, but Chromium
    doesn't expose intermediate redirect-chain hops as separately
    interceptable requests (verified live - see ssrf_guard.py's module
    docstring). The post-navigation final-URL check is what actually catches
    a redirect that lands somewhere bad, and it's unconditional now.
    """
    async def fake_assert_public_url(url):
        if "169.254" in url:
            raise SSRFBlockedError(f"blocked: {url}")

    monkeypatch.setattr("app.fetch.assert_public_url", fake_assert_public_url)

    redirected = _ok_page(html="<html><body>internal</body></html>", url="http://169.254.169.254/secret")
    browser = _make_browser_with_pages(lambda i: redirected)
    fetcher = PageFetcher(browser, max_attempts=3, base_backoff_seconds=0.01)

    result = await fetcher.fetch("https://x.example/")

    assert result.ok is False
    assert result.blocked_ssrf is True
    assert result.attempts == 1  # blocked outright, not retried like a reliability failure


@pytest.mark.asyncio
async def test_successful_fetch_reports_ssrf_guard_stats(monkeypatch):
    """FetchResult should carry positive, countable confirmation the guard
    ran - not just an absence of blocked requests.
    """
    from app.security.ssrf_guard import SSRFGuardStats

    async def fake_install_ssrf_guard(context):
        return SSRFGuardStats(validated=7, blocked=1)

    monkeypatch.setattr("app.fetch.install_ssrf_guard", fake_install_ssrf_guard)

    browser = _make_browser_with_pages(lambda i: _ok_page())
    fetcher = PageFetcher(browser, max_attempts=3, base_backoff_seconds=0.01)
    result = await fetcher.fetch("https://x.example/")

    assert result.ok is True
    assert result.ssrf_requests_validated == 7
    assert result.ssrf_requests_blocked == 1


@pytest.mark.asyncio
async def test_exhausted_retries_still_reports_nonzero_ssrf_validated_count(monkeypatch):
    """Follow-up round, Part 2: a fetch that times out on every attempt
    still had its navigation request(s) legitimately validated by the SSRF
    guard before each one failed to complete - the final CANNOT_VERIFY
    result must accumulate that across every attempt, not report 0 (which
    would misleadingly suggest the guard never ran on this fetch at all)."""
    from app.security.ssrf_guard import SSRFGuardStats

    call_count = {"n": 0}

    async def fake_install_ssrf_guard(context):
        call_count["n"] += 1
        # A different (nonzero) count per attempt - proves accumulation
        # across attempts, not just echoing the last one.
        return SSRFGuardStats(validated=call_count["n"], blocked=0)

    monkeypatch.setattr("app.fetch.install_ssrf_guard", fake_install_ssrf_guard)

    browser = _make_browser_with_pages(lambda i: _timeout_page())
    fetcher = PageFetcher(browser, max_attempts=3, base_backoff_seconds=0.01)
    result = await fetcher.fetch("https://x.example/")

    assert result.ok is False
    assert result.cannot_verify is True
    assert result.ssrf_requests_validated == 1 + 2 + 3  # summed across all 3 attempts


@pytest.mark.asyncio
async def test_ssrf_blocked_url_short_circuits_before_touching_the_browser(monkeypatch):
    async def fake_assert_public_url(url):
        raise SSRFBlockedError(f"blocked: {url}")
    monkeypatch.setattr("app.fetch.assert_public_url", fake_assert_public_url)

    browser = MagicMock()
    browser.new_context = AsyncMock(side_effect=AssertionError("must not touch the browser for a blocked URL"))
    fetcher = PageFetcher(browser, max_attempts=3, base_backoff_seconds=0.01)

    result = await fetcher.fetch("http://169.254.169.254/latest/meta-data/")
    assert result.ok is False
    assert result.blocked_ssrf is True
    assert result.attempts == 1  # the block verdict itself is attempt 1, just never touches the browser
    assert result.cannot_verify is False  # blocked-by-design, not a reliability failure
    browser.new_context.assert_not_called()


@pytest.mark.asyncio
async def test_transient_dns_failure_is_retried_not_treated_as_a_block(monkeypatch):
    """Regression: a real audit hit a transient DNS hiccup partway through a
    218-page crawl. Every page fetched after that point was permanently
    marked blocked_ssrf (never retried) instead of retried like any other
    network failure, and three required policy pages that genuinely exist
    were reported as 'confirmed missing' - see DNSResolutionError's
    docstring. This proves a transient DNS failure now recovers on retry.
    """
    calls = {"n": 0}

    async def flaky_then_ok(url):
        calls["n"] += 1
        if calls["n"] < 3:
            raise DNSResolutionError(f"Could not resolve host {url!r}: [Errno 11001] getaddrinfo failed")
        return None

    monkeypatch.setattr("app.fetch.assert_public_url", flaky_then_ok)
    browser = _make_browser_with_pages(lambda i: _ok_page())
    fetcher = PageFetcher(browser, max_attempts=5, base_backoff_seconds=0.01)

    result = await fetcher.fetch("https://x.example/")
    assert result.ok is True
    assert result.blocked_ssrf is False
    # 2 failed pre-navigation checks + 1 successful pre-navigation check +
    # 1 successful post-navigation (final-URL) check = 4 total calls.
    assert calls["n"] == 4


@pytest.mark.asyncio
async def test_dns_failure_exhausting_all_retries_is_cannot_verify_not_blocked(monkeypatch):
    async def always_dns_fails(url):
        raise DNSResolutionError(f"Could not resolve host {url!r}: [Errno 11001] getaddrinfo failed")

    monkeypatch.setattr("app.fetch.assert_public_url", always_dns_fails)
    browser = MagicMock()
    browser.new_context = AsyncMock(side_effect=AssertionError("must not touch the browser while DNS keeps failing"))
    fetcher = PageFetcher(browser, max_attempts=3, base_backoff_seconds=0.01)

    result = await fetcher.fetch("https://x.example/")
    assert result.ok is False
    assert result.blocked_ssrf is False
    assert result.cannot_verify is True  # the whole point: a DNS hiccup must never look like "confirmed missing"
    assert result.attempts == 3


@pytest.mark.asyncio
async def test_every_context_gets_the_ssrf_route_guard_installed():
    context = MagicMock()
    context.new_page = AsyncMock(return_value=_ok_page())
    context.close = AsyncMock()
    context.route = AsyncMock()
    context.add_init_script = AsyncMock()

    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    fetcher = PageFetcher(browser, max_attempts=3, base_backoff_seconds=0.01)

    await fetcher.fetch("https://x.example/")

    context.route.assert_awaited_once()
    assert context.route.await_args.args[0] == "**/*"


# --- Part 1: HTTP-status-aware retry/backoff -------------------------------

@pytest.mark.asyncio
async def test_429_backs_off_using_retry_after_header_then_succeeds():
    rate_limited = _status_page(429)
    rate_limited.response = None  # not used directly; header_value is what matters
    ok = _ok_page()
    pages = [rate_limited, ok]
    browser = _make_browser_with_pages(lambda i: pages[i])
    fetcher = PageFetcher(browser, max_attempts=3, base_backoff_seconds=10.0)  # huge exponential backoff...

    # ...proves the real (short) Retry-After delay was used instead, not the
    # exponential fallback - patch goto's response to carry the header.
    async def goto_with_retry_after(url, timeout, wait_until):
        response = MagicMock()
        response.status = 429
        response.header_value = AsyncMock(return_value="0")  # "wait 0s" - keeps the test fast
        return response

    rate_limited.goto = AsyncMock(side_effect=goto_with_retry_after)

    import time
    start = time.monotonic()
    result = await fetcher.fetch("https://x.example/")
    elapsed = time.monotonic() - start

    assert result.ok is True
    assert result.attempts == 2
    assert elapsed < 5.0  # would be >=10s if the exponential fallback had been used instead


@pytest.mark.asyncio
async def test_429_exhausted_is_reported_as_rate_limited():
    browser = _make_browser_with_pages(lambda i: _status_page(429))
    fetcher = PageFetcher(browser, max_attempts=2, base_backoff_seconds=0.01)
    result = await fetcher.fetch("https://x.example/")
    assert result.ok is False
    assert result.rate_limited is True
    assert result.cannot_verify is True
    assert result.failure_category == "rate_limited"


@pytest.mark.asyncio
async def test_403_stops_retrying_before_max_attempts_and_reports_bot_blocked():
    """403/401 must not be hammered with identical requests up to the full
    max_attempts budget - capped lower (max_bot_block_attempts) instead."""
    calls = {"n": 0}

    def factory(i):
        calls["n"] += 1
        return _status_page(403)

    browser = _make_browser_with_pages(factory)
    fetcher = PageFetcher(browser, max_attempts=5, base_backoff_seconds=0.01, max_bot_block_attempts=2)
    result = await fetcher.fetch("https://x.example/private")
    assert result.ok is False
    assert result.likely_bot_blocked is True
    assert result.failure_category == "bot_blocked"
    assert calls["n"] == 2  # stopped at the cap, not max_attempts=5


@pytest.mark.asyncio
async def test_401_is_categorized_as_bot_blocked_too():
    browser = _make_browser_with_pages(lambda i: _status_page(401))
    fetcher = PageFetcher(browser, max_attempts=2, base_backoff_seconds=0.01, max_bot_block_attempts=2)
    result = await fetcher.fetch("https://x.example/admin")
    assert result.likely_bot_blocked is True
    assert result.failure_category == "bot_blocked"


@pytest.mark.asyncio
async def test_503_still_retries_and_recovers_like_before():
    pages = [_status_page(503), _ok_page()]
    browser = _make_browser_with_pages(lambda i: pages[i])
    fetcher = PageFetcher(browser, max_attempts=3, base_backoff_seconds=0.01)
    result = await fetcher.fetch("https://x.example/")
    assert result.ok is True
    assert result.attempts == 2


@pytest.mark.asyncio
async def test_503_exhausted_retries_gets_a_named_category_not_unspecified():
    """Follow-up round: a real 503 on a dynamic checkout URL that never
    recovered was reported as "unreachable ... for an unspecified reason" -
    the retry logic already handled 503 correctly (retries, gives up after
    max_attempts), but every HTTP-status failure not covered by a more
    specific category (429/403/401) fell into the generic "unknown" bucket,
    which FetchResult had no matching boolean field for at all - so even a
    later attempt to name it more specifically would have silently done
    nothing. Must report failure_category="http_error", not "unknown"."""
    browser = _make_browser_with_pages(lambda i: _status_page(503))
    fetcher = PageFetcher(browser, max_attempts=3, base_backoff_seconds=0.01)
    result = await fetcher.fetch("https://x.example/checkout?wc-quick-buy-now=228")
    assert result.ok is False
    assert result.http_error is True
    assert result.failure_category == "http_error"


@pytest.mark.asyncio
async def test_network_level_timeout_exhausted_is_categorized_as_network_error():
    browser = _make_browser_with_pages(lambda i: _timeout_page())
    fetcher = PageFetcher(browser, max_attempts=2, base_backoff_seconds=0.01)
    result = await fetcher.fetch("https://x.example/")
    assert result.network_error is True
    assert result.failure_category == "network_error"


@pytest.mark.asyncio
async def test_per_domain_min_delay_spaces_out_requests_to_the_same_domain():
    import time

    pages = [_status_page(503), _ok_page()]
    browser = _make_browser_with_pages(lambda i: pages[i])
    fetcher = PageFetcher(browser, max_attempts=2, base_backoff_seconds=0.01, domain_min_delay_seconds=0.3)

    start = time.monotonic()
    result = await fetcher.fetch("https://x.example/")
    elapsed = time.monotonic() - start

    assert result.ok is True
    assert elapsed >= 0.3  # the second request had to wait for the domain's min delay


@pytest.mark.asyncio
async def test_zero_domain_min_delay_is_a_no_op_by_default():
    """Default PageFetcher construction (as used by most tests/unit callers)
    must stay fast - the politeness delay is opt-in via Settings, wired up
    by app.site_mapper, not a PageFetcher default."""
    import time

    browser = _make_browser_with_pages(lambda i: _ok_page())
    fetcher = PageFetcher(browser, max_attempts=3, base_backoff_seconds=0.01)
    start = time.monotonic()
    await fetcher.fetch("https://x.example/")
    await fetcher.fetch("https://x.example/other-page")
    assert time.monotonic() - start < 0.5


# --- Part 2: anti-bot interstitials and cookie-consent banners -------------

_CHALLENGE_HTML = "<html><body><h1>Just a moment...</h1><p>Checking your browser before accessing x.example.</p></body></html>"
_REAL_CONTENT_HTML = "<html><body><h1>Welcome to the store</h1><p>Lots of real product content here.</p></body></html>"


def _challenge_then_resolves_page():
    """A page whose first content() call looks like a Cloudflare interstitial,
    and whose subsequent content() calls (post-wait) look like the real page -
    simulates the JS challenge resolving on its own, which Playwright (a real
    browser) can plausibly let happen."""
    page = MagicMock()
    response = MagicMock()
    response.status = 200
    page.goto = AsyncMock(return_value=response)
    page.wait_for_load_state = AsyncMock()
    page.content = AsyncMock(side_effect=[_CHALLENGE_HTML, _REAL_CONTENT_HTML])
    page.inner_text = AsyncMock(return_value="Welcome to the store")
    page.url = "https://x.example/"
    return page


@pytest.mark.asyncio
async def test_challenge_interstitial_that_resolves_is_treated_as_success():
    page = _challenge_then_resolves_page()
    browser = _make_browser_with_pages(lambda i: page)
    fetcher = PageFetcher(browser, max_attempts=2, base_backoff_seconds=0.01, challenge_wait_seconds=3.0)
    result = await fetcher.fetch("https://x.example/")
    assert result.ok is True
    assert result.html == _REAL_CONTENT_HTML


@pytest.mark.asyncio
async def test_challenge_interstitial_that_never_resolves_is_bot_blocked():
    page = _status_page(200, html=_CHALLENGE_HTML)
    page.url = "https://x.example/"
    browser = _make_browser_with_pages(lambda i: page)
    fetcher = PageFetcher(browser, max_attempts=1, base_backoff_seconds=0.01, challenge_wait_seconds=0.05)
    result = await fetcher.fetch("https://x.example/")
    assert result.ok is False
    assert result.likely_bot_blocked is True
    assert result.failure_category == "bot_blocked"


@pytest.mark.asyncio
async def test_full_page_captcha_block_is_categorized_distinctly_from_generic_bot_block():
    captcha_html = (
        "<html><body><h1>Please verify you are human</h1>"
        "<div class='g-recaptcha' data-sitekey='x'></div>"
        "<script src='https://www.google.com/recaptcha/api.js'></script></body></html>"
    )
    page = _status_page(200, html=captcha_html)
    page.url = "https://x.example/"
    browser = _make_browser_with_pages(lambda i: page)
    fetcher = PageFetcher(browser, max_attempts=1, base_backoff_seconds=0.01, challenge_wait_seconds=0.05)
    result = await fetcher.fetch("https://x.example/")
    assert result.ok is False
    assert result.likely_captcha_blocked is True
    assert result.failure_category == "captcha_blocked"
    assert result.attempts == 1  # never retried a CAPTCHA wall


@pytest.mark.asyncio
async def test_normal_page_with_embedded_recaptcha_widget_is_not_treated_as_blocked():
    """A contact/newsletter form legitimately embedding a reCAPTCHA widget
    must not be misdetected as a full-page CAPTCHA block - only a page whose
    content is otherwise thin (or explicitly says "verify you're human")
    counts as blocked."""
    from app.fetch import _looks_like_captcha, _looks_like_challenge

    normal_form_html = (
        "<html><body><header>Site Nav</header><main><h1>Contact Us</h1>"
        + "<p>Lots of unrelated page content. " * 40
        + "<form><input name='email'><div class='g-recaptcha' data-sitekey='x'></div>"
        "<script src='https://www.google.com/recaptcha/api.js'></script></form></main>"
        "<footer>Footer content</footer></body></html>"
    )
    assert _looks_like_captcha(normal_form_html) is False
    assert _looks_like_challenge(normal_form_html) is False


@pytest.mark.asyncio
async def test_cookie_consent_accept_button_is_clicked_when_present():
    page = _ok_page()
    accept_locator = MagicMock()
    accept_locator.count = AsyncMock(return_value=1)
    accept_locator.is_visible = AsyncMock(return_value=True)
    accept_locator.click = AsyncMock()
    first_locator = MagicMock()
    first_locator.first = accept_locator
    page.locator = MagicMock(return_value=first_locator)
    page.wait_for_timeout = AsyncMock()

    browser = _make_browser_with_pages(lambda i: page)
    fetcher = PageFetcher(browser, max_attempts=1, base_backoff_seconds=0.01)
    result = await fetcher.fetch("https://x.example/")

    assert result.ok is True
    accept_locator.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_proxy_rotator_configures_each_new_browser_context():
    """Part 5.2: when a proxy_rotator is configured, every new browser
    context (one per fetch attempt) must be created with that attempt's
    proxy - and must NOT pass a proxy at all when unconfigured (the default,
    unchanged behavior)."""
    from app.proxy_config import ProxyConfig, ProxyRotator

    seen_kwargs = []

    async def new_context(**kwargs):
        seen_kwargs.append(kwargs)
        context = MagicMock()
        context.new_page = AsyncMock(return_value=_ok_page())
        context.close = AsyncMock()
        context.route = AsyncMock()
        context.add_init_script = AsyncMock()
        return context

    browser = MagicMock()
    browser.new_context = new_context

    rotator = ProxyRotator([ProxyConfig(server="http://proxy.example:8000", username="u", password="p")])
    fetcher = PageFetcher(browser, max_attempts=1, base_backoff_seconds=0.01, proxy_rotator=rotator)
    await fetcher.fetch("https://x.example/")

    assert seen_kwargs[0]["proxy"] == {"server": "http://proxy.example:8000", "username": "u", "password": "p"}


@pytest.mark.asyncio
async def test_no_proxy_configured_means_no_proxy_kwarg_at_all():
    seen_kwargs = []

    async def new_context(**kwargs):
        seen_kwargs.append(kwargs)
        context = MagicMock()
        context.new_page = AsyncMock(return_value=_ok_page())
        context.close = AsyncMock()
        context.route = AsyncMock()
        context.add_init_script = AsyncMock()
        return context

    browser = MagicMock()
    browser.new_context = new_context

    fetcher = PageFetcher(browser, max_attempts=1, base_backoff_seconds=0.01)
    await fetcher.fetch("https://x.example/")

    assert "proxy" not in seen_kwargs[0]


@pytest.mark.asyncio
async def test_cookie_consent_dismissal_failure_never_breaks_the_fetch():
    """Best-effort only: any error while looking for/clicking a consent
    control must be swallowed, never fail the page fetch."""
    page = _ok_page()
    page.locator = MagicMock(side_effect=RuntimeError("boom"))

    browser = _make_browser_with_pages(lambda i: page)
    fetcher = PageFetcher(browser, max_attempts=1, base_backoff_seconds=0.01)
    result = await fetcher.fetch("https://x.example/")
    assert result.ok is True
