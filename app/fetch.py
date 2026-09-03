"""Step 3: resilient, JS-rendered page fetch.

This is the reliability core the whole project depends on. Every page fetch:
  - renders through a real headless browser (Playwright), so client-side/SPA
    content is present in the DOM snapshot, not just the initial HTML,
  - waits for the network to go idle (falling back to `domcontentloaded` +
    a settle delay if network never quiesces, e.g. persistent analytics
    beacons), instead of a single fixed timeout,
  - gives a genuine anti-bot JS interstitial (Cloudflare-style "checking
    your browser") a real chance to resolve - Playwright drives a real
    browser, unlike a bare HTTP client, so this can plausibly pass -
    before treating the page as failed,
  - best-effort dismisses a cookie-consent banner so it can't obscure
    content from subsequent checks on stores that genuinely gate content
    behind consent,
  - is HTTP-status-aware on retry: 429 backs off (respecting Retry-After
    when present), 403/401 gets a capped number of attempts rather than
    hammering an identical request against what's likely a block, 404/410
    is confirmed-not-found and never retried, everything else retries with
    exponential backoff,
  - applies a per-domain minimum delay between requests so this crawler's
    own pace doesn't trip a target site's rate limiting,
  - is only marked CANNOT_VERIFY after every attempt is exhausted, and
    always carries a specific, honest failure_category (rate-limited,
    bot-blocked, CAPTCHA-blocked, network-level, etc.) rather than a
    generic "could not verify" - see FAILURE_CATEGORY_LABELS,
  - has all three SSRF guard layers active - upfront, per-request
    interception, and post-navigation final-URL check - on every attempt,
    with no toggle and no fallback path that skips any of them (see
    app/security/ssrf_guard.py's module docstring for why three, not two).

A single shared Playwright browser instance should be reused across many
fetches (launching a browser per page is what makes naive crawlers slow);
`PageFetcher` takes an already-launched `Browser` so the caller controls
that lifecycle.

Known, deliberate limits (see the follow-up brief this round implements):
CAPTCHA solving and residential/rotating-proxy IP evasion are explicitly
out of scope - both would cross from "resilient, well-behaved crawler" into
"actively defeating a site's anti-automation defenses." A CAPTCHA is
detected and reported, never solved.
"""
from __future__ import annotations

import asyncio
import logging
import re
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from playwright.async_api import Browser, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel

from app.proxy_config import ProxyRotator, to_playwright_proxy
from app.security.ssrf_guard import DNSResolutionError, SSRFBlockedError, assert_public_url, install_ssrf_guard

logger = logging.getLogger("gmc_audit.fetch")

# "Mozilla/5.0 (compatible; ...)" prefix kept for site compatibility (many
# themes/CDNs serve degraded content to non-browser-looking UAs) - same
# identifiable pattern real crawlers like Googlebot use. Otherwise matches
# app.security.ssrf_guard.GMC_AUDIT_USER_AGENT for consistency.
BROWSER_USER_AGENT = "Mozilla/5.0 (compatible; gmc-compliance-auditor/0.1; +automated GMC policy compliance audit)"

# A default Playwright context has no viewport set and leaves
# navigator.webdriver = true - both are common headless-automation signals
# that some hosts' anti-bot layers act on by silently truncating the
# response (observed live: a real WooCommerce store served a body-less,
# head-only HTML document - no error, no block page, just an incomplete
# document - to a bare Playwright context, while curl with the same
# identifiable UA and a "stealthed" context both got the full page). This
# isn't UA spoofing - BROWSER_USER_AGENT stays honest and identifiable -
# it's presenting as an ordinary browser window instead of an automation
# harness's default (no) viewport.
STEALTH_VIEWPORT = {"width": 1366, "height": 768}
STEALTH_INIT_SCRIPT = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"


# --- Honest failure categorization (broader crawl-robustness round) ------
#
# Every non-success fetch gets one specific reason, not a generic "could not
# verify" - this is what lets a report say *why* a page couldn't be checked
# (ask the merchant to allowlist the User-Agent vs. "this is a transient
# network blip, re-run later" vs. "this is a real 404") instead of leaving
# the reader to guess. See app.models.CrawledPage.failure_category and
# app.checks.deterministic/business_identity for how this flows into
# findings, and app.report for how it's surfaced.
FAILURE_CATEGORY_LABELS: dict[str, str] = {
    "not_found": "confirmed not found (HTTP 404/410) - a real broken link, not a reliability failure",
    "blocked_ssrf": "refused by this tool's own SSRF guard (resolved to a non-public address)",
    "captcha_blocked": "blocked by a CAPTCHA challenge - this tool does not attempt to solve CAPTCHAs, by design",
    "bot_blocked": "likely blocked by bot-protection (a JS interstitial that never resolved, or a 403/401 response repeated on retry)",
    "rate_limited": "rate-limited by the site (HTTP 429) even after backing off and retrying",
    "network_error": "a DNS/connection/timeout-level network failure, not an HTTP response from the site",
    # A real HTTP response (e.g. a 503 that never recovered, or an
    # unexpected 4xx/5xx not covered by a more specific category above) -
    # distinct from "unknown": the exact status code is always in the
    # accompanying fetch-error text (see _failure_reason_clause), so this
    # is a *named*, specific reason, not a shrug. Found live: a real 503 on
    # a dynamic checkout URL was rendering as "unreachable ... for an
    # unspecified reason" because every HTTP-status response that wasn't
    # one of the specifically-handled codes fell into the generic "unknown"
    # bucket below, even though the retry logic already handled it
    # correctly (retried, then gave up) - only the category label was wrong.
    "http_error": "a real HTTP error response from the site (see the exact status code in the fetch error below), not a network-level failure",
    "unknown": "unreachable after retries for an unspecified reason",
}

FAILURE_CATEGORY_SHORT_LABELS: dict[str, str] = {
    "not_found": "not found",
    "blocked_ssrf": "blocked by SSRF guard",
    "captcha_blocked": "CAPTCHA-blocked",
    "bot_blocked": "bot-blocked",
    "rate_limited": "rate-limited",
    "network_error": "network error",
    "http_error": "HTTP error",
    "unknown": "unknown reason",
}

# Actionable next step per category - reused by app.checks and app.report so
# a reader knows whether anything is actually actionable (e.g. asking the
# merchant to allowlist this tool) versus a hard block.
FAILURE_CATEGORY_RECOMMENDATIONS: dict[str, str] = {
    "captcha_blocked": "Ask the merchant to allowlist this tool's User-Agent, or complete a manual check for this page - this tool will not attempt to solve the CAPTCHA.",
    "bot_blocked": "Ask the merchant to allowlist this tool's User-Agent (or IP), or complete a manual check for this page.",
    "rate_limited": "Re-run the audit later, or ask the merchant to allowlist this tool so its request pace doesn't trigger rate limiting.",
    "network_error": "This may be a transient DNS/connectivity issue - re-run the audit; if it persists, confirm the site is actually online.",
    "not_found": "Confirm whether the link/URL is correct or should be removed/redirected.",
    "blocked_ssrf": "Confirm the URL is correct - this tool refuses to fetch addresses that resolve to a non-public/internal IP, for safety.",
    "http_error": "Check the site/server for this specific status code and URL - if it's a real 5xx, re-run once the underlying issue clears; if an unexpected 4xx, confirm the URL is correct.",
    "unknown": "Re-run the audit; if this persists, investigate why the site could not be reached.",
}


def classify_httpx_exception(exc: Exception) -> str:
    """Same failure-category vocabulary as PageFetcher's own retry logic
    (FAILURE_CATEGORY_LABELS above), for the handful of *other* places in
    this codebase that make a direct httpx call outside PageFetcher (image
    probing, form-action reachability probing) - failure-reporting
    specificity audit, follow-up round Part 1.3. Found live (by code
    inspection, not a running store) that each of these had independently
    grown its own bare `except httpx.HTTPError as exc: ...str(exc)...`
    handler with a single guessed recommendation that doesn't fit every
    cause (e.g. "the host may be blocking automated requests" is wrong
    advice for a plain timeout) - and, for one of them, didn't distinguish
    DNSResolutionError from a real SSRFBlockedError at all, reproducing the
    exact false-positive class already found and fixed once in PageFetcher
    itself (a transient DNS hiccup reported as a confirmed block). One
    shared classifier instead of three independent reimplementations.
    """
    if isinstance(exc, DNSResolutionError):
        return "network_error"
    if isinstance(exc, SSRFBlockedError):
        return "blocked_ssrf"
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return "network_error"
    return "unknown"


class FetchResult(BaseModel):
    url: str
    ok: bool
    status: int | None = None
    html: str | None = None
    text: str | None = None
    final_url: str | None = None
    attempts: int = 0
    error: str | None = None
    # True only for a confirmed non-2xx/3xx response (e.g. 404/410) that is
    # not worth retrying - a real broken link, not a reliability failure.
    confirmed_not_found: bool = False
    # True when the URL (or a redirect target) resolved to a non-public
    # address and was refused - never retried, never CANNOT_VERIFY.
    blocked_ssrf: bool = False
    # HTTP-status-aware failure categories: each distinct from a generic
    # reliability failure, so retry behavior AND report language can differ
    # per cause instead of treating every non-2xx the same way.
    rate_limited: bool = False
    likely_bot_blocked: bool = False
    likely_captcha_blocked: bool = False
    network_error: bool = False
    # A real HTTP response whose status code isn't one of the other, more
    # specific categories above (e.g. a 503 that never recovered) - see
    # FAILURE_CATEGORY_LABELS["http_error"] for why this needs its own flag
    # rather than falling through to the generic "unknown" bucket.
    http_error: bool = False
    # Positive confirmation the per-request SSRF guard actually ran on this
    # fetch's final (successful) attempt, and how many requests it checked -
    # always populated on a successful fetch, since the guard is always on.
    ssrf_requests_validated: int = 0
    ssrf_requests_blocked: int = 0

    @property
    def cannot_verify(self) -> bool:
        return not self.ok and not self.confirmed_not_found and not self.blocked_ssrf

    @property
    def failure_category(self) -> str | None:
        """One of FAILURE_CATEGORY_LABELS' keys, or None when ok. A single
        fetch can only have one "final" reason it gave up, even though
        different attempts along the way may have hit different causes -
        priority order below reflects which cause is most useful to surface."""
        if self.ok:
            return None
        if self.confirmed_not_found:
            return "not_found"
        if self.blocked_ssrf:
            return "blocked_ssrf"
        if self.likely_captcha_blocked:
            return "captcha_blocked"
        if self.likely_bot_blocked:
            return "bot_blocked"
        if self.rate_limited:
            return "rate_limited"
        if self.network_error:
            return "network_error"
        if self.http_error:
            return "http_error"
        return "unknown"


def _looks_bodyless(html: str | None) -> bool:
    """True for a "successful" (2xx) navigation whose captured document has
    no <body> at all. A real WooCommerce store's response was observed to
    come back exactly like this whenever Playwright's route() interception
    used route.continue_() - fixed in install_ssrf_guard by using
    route.fetch()+route.fulfill() instead, verified across repeated live
    runs. Kept as a retry trigger regardless, since a genuinely truncated
    response for unrelated reasons (server flakiness) should still be
    retried rather than silently accepted as a real page.
    """
    return bool(html) and "<body" not in html.lower()


# --- Anti-bot JS interstitial / CAPTCHA detection -------------------------
#
# Tier A: phrases that essentially only ever appear on a full-page "checking
# your browser"-style interstitial - safe to trigger on the phrase alone.
_CHALLENGE_PHRASES = (
    "checking your browser",
    "just a moment...",
    "attention required! | cloudflare",
    "ddos-guard",
    "verifying you are human",
    "please wait while we verify",
    "needs to review the security of your connection",
    "enable javascript and cookies to continue",
    "checking if the site connection is secure",
    "please stand by, while we are checking your browser",
    "please complete the security check to access",
    "unusual traffic from your computer network",  # Google's own bot-check page
)

# Tier B: CAPTCHA widget/script signatures - ambiguous on their own (a
# perfectly normal contact/newsletter form can legitimately embed a
# reCAPTCHA widget), so only treated as evidence of a *blocking* CAPTCHA
# when combined with a thin page body (see _MAX_INTERSTITIAL_BODY_CHARS) or
# an explicit "verify you're human" phrase - not from the widget alone.
_CAPTCHA_WIDGET_SIGNATURES = (
    "g-recaptcha", "recaptcha/api.js", "www.google.com/recaptcha",
    "hcaptcha.com/1/api.js", "h-captcha",
    "challenges.cloudflare.com/turnstile", "cf-turnstile",
    "captcha-delivery.com",  # DataDome
    "funcaptcha", "arkoselabs.com",
)
_CAPTCHA_BLOCK_PHRASES = ("verify you are human", "complete the security check", "i'm not a robot")

# A real full-page interstitial/CAPTCHA screen has almost no other content
# besides the challenge widget and a sentence or two - this distinguishes
# "the whole page is a block screen" from "this normal page happens to
# embed a captcha widget somewhere in a form".
_MAX_INTERSTITIAL_BODY_CHARS = 1200
_TAG_RE = re.compile(r"<[^>]+>")


def _visible_text_length(html: str) -> int:
    return len(_TAG_RE.sub(" ", html))


def _looks_like_challenge(html: str | None) -> bool:
    if not html:
        return False
    lowered = html.lower()
    if any(p in lowered for p in _CHALLENGE_PHRASES):
        return True
    if any(sig in lowered for sig in _CAPTCHA_WIDGET_SIGNATURES) and _visible_text_length(html) < _MAX_INTERSTITIAL_BODY_CHARS:
        return True
    return False


def _looks_like_captcha(html: str | None) -> bool:
    """Narrower than _looks_like_challenge: true only when this specifically
    looks like a full-page CAPTCHA block (not just a generic "checking your
    browser" interstitial with no CAPTCHA at all) - lets the report give a
    more specific, actionable message (never attempts to solve it)."""
    if not html:
        return False
    lowered = html.lower()
    if not any(sig in lowered for sig in _CAPTCHA_WIDGET_SIGNATURES):
        return False
    return _visible_text_length(html) < _MAX_INTERSTITIAL_BODY_CHARS or any(p in lowered for p in _CAPTCHA_BLOCK_PHRASES)


# --- Cookie-consent banner dismissal --------------------------------------
#
# Best-effort only: some stores (particularly EU-facing ones) genuinely gate
# real content behind a consent banner, or render an overlay that blocks
# clicks on the underlying page. This is deliberately conservative - a
# banner it doesn't recognize is left alone rather than risk clicking the
# wrong control (e.g. "reject" instead of "accept", or an unrelated button
# that happens to match a generic text pattern).
_CONSENT_SELECTORS = (
    "#onetrust-accept-btn-handler",                                   # OneTrust
    "button#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",   # Cookiebot
    ".cc-btn.cc-allow", ".cc-allow",                                   # cookieconsent.js
    "[data-cky-tag='accept-button']",                                  # CookieYes
    "#accept-cookies", "#cookie-accept", ".cookie-accept",
    "[data-testid='cookie-accept-all']",
    "button[aria-label='Accept all']",
    "button[aria-label='Accept All']",
    "button[aria-label='Accept all cookies']",
)
_CONSENT_TEXT_PATTERN = re.compile(r"^(accept all( cookies)?|accept cookies|i agree|allow all|got it|ok(ay)?)$", re.IGNORECASE)


async def _dismiss_cookie_consent(page) -> None:
    """Click a recognized "accept" control if one is visible. Never raises -
    this is a non-critical best-effort step; any failure (control not found,
    not clickable, page torn down mid-check) is logged and swallowed."""
    try:
        for selector in _CONSENT_SELECTORS:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            if not await locator.is_visible():
                continue
            await locator.click(timeout=1500)
            await page.wait_for_timeout(300)
            return
        locator = page.get_by_role("button", name=_CONSENT_TEXT_PATTERN).first
        if await locator.count() and await locator.is_visible():
            await locator.click(timeout=1500)
            await page.wait_for_timeout(300)
    except Exception as exc:  # pragma: no cover - purely defensive, any cause is non-fatal
        logger.debug("Cookie-consent dismissal skipped/failed (non-fatal): %s", exc)


async def _parse_retry_after(response) -> float | None:
    """Seconds to wait per a 429/503 response's Retry-After header (either a
    plain integer or an HTTP-date), or None if absent/unparseable."""
    try:
        raw = await response.header_value("retry-after")
    except Exception:
        return None
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return float(raw)
    try:
        dt = parsedate_to_datetime(raw)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


class _RetryableFetchFailure(RuntimeError):
    """Internal control-flow exception carrying *why* a fetch attempt failed,
    so the retry loop can apply status-aware backoff (Part 1) and the final
    FetchResult can report a specific, honest failure category (Part 4)
    instead of a generic "could not verify"."""

    def __init__(self, message: str, category: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.retry_after = retry_after


class _DomainThrottle:
    """Per-domain politeness delay: ensures this crawler's own request pace
    to any single domain never drops below `min_delay_seconds`, so it
    doesn't trip a target site's own rate limiting. A no-op when
    min_delay_seconds is 0 (the default for a bare PageFetcher/tests;
    app.site_mapper wires up the real configured value - see
    app.config.Settings.crawl_domain_min_delay_seconds). Concurrent callers
    for the same domain are queued (each reserves the next slot under the
    lock) rather than all waking up together and violating the delay.
    """

    def __init__(self, min_delay_seconds: float) -> None:
        self.min_delay_seconds = min_delay_seconds
        self._next_available_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def wait(self, domain: str) -> None:
        if self.min_delay_seconds <= 0:
            return
        async with self._lock:
            now = asyncio.get_event_loop().time()
            start_at = max(now, self._next_available_at.get(domain, now))
            self._next_available_at[domain] = start_at + self.min_delay_seconds
            delay = start_at - now
        if delay > 0:
            await asyncio.sleep(delay)


class PageFetcher:
    def __init__(
        self,
        browser: Browser,
        max_attempts: int = 3,
        base_backoff_seconds: float = 1.5,
        max_backoff_seconds: float = 30.0,
        nav_timeout_ms: int = 20_000,
        settle_timeout_ms: int = 5_000,
        challenge_wait_seconds: float = 6.0,
        max_bot_block_attempts: int = 2,
        domain_min_delay_seconds: float = 0.0,
        proxy_rotator: ProxyRotator | None = None,
    ) -> None:
        self.browser = browser
        self.max_attempts = max_attempts
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.nav_timeout_ms = nav_timeout_ms
        self.settle_timeout_ms = settle_timeout_ms
        self.challenge_wait_seconds = challenge_wait_seconds
        # 403/401 (and an interstitial that never resolves) stop retrying
        # after this many hits, deliberately lower than max_attempts by
        # default - a repeated identical request against what's likely a
        # block is not worth hammering the site with (Part 1.1).
        self.max_bot_block_attempts = max_bot_block_attempts
        self._throttle = _DomainThrottle(domain_min_delay_seconds)
        # Opt-in BYO proxy support (Part 5.2, app.proxy_config) - None (the
        # default) means every context is created with no proxy at all,
        # unchanged from before this existed. A new context is created per
        # fetch attempt already (below), so picking the next pool entry per
        # attempt gives real rotation across a multi-endpoint pool for free.
        self._proxy_rotator = proxy_rotator

    async def _wait_for_challenge_to_resolve(self, page, initial_html: str) -> str:
        """Cloudflare/DDoS-Guard-style JS interstitials exist specifically to
        filter out non-browser clients; since Playwright drives a real
        browser (not a bare HTTP client) it can plausibly get through one
        automatically once the interstitial's own challenge script runs and
        navigates away. Poll for a few seconds, watching for the DOM to
        change away from a known challenge/CAPTCHA signature, before giving
        up - this is the explicit "genuine chance to resolve" step; the
        plain networkidle wait above does NOT reliably cover this on its
        own, since a challenge page can sit "idle" (its own polling/timer
        JS aside) for its whole multi-second countdown before it navigates.
        """
        html = initial_html
        poll_interval = 1.0
        elapsed = 0.0
        while (_looks_like_challenge(html) or _looks_like_captcha(html)) and elapsed < self.challenge_wait_seconds:
            try:
                await page.wait_for_load_state("networkidle", timeout=int(poll_interval * 1000))
            except PlaywrightTimeoutError:
                pass
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            try:
                html = await page.content()
            except PlaywrightError:
                break
        return html

    async def fetch(self, url: str) -> FetchResult:
        last_error: str | None = None
        last_status: int | None = None
        last_category: str = "unknown"
        bot_block_hits = 0
        domain = urlparse(url).hostname or url
        # Accumulated across every attempt that got far enough to create a
        # browser context (i.e. passed the upfront per-attempt
        # assert_public_url check) - a retried/failed attempt's per-request
        # guard activity is real work the guard did and must not be
        # silently dropped just because that attempt didn't end in a
        # successful page load. Folded in per-attempt via the `finally`
        # block below; a `return` inside the loop adds the *current*
        # attempt's guard_stats explicitly, since finally hasn't run for it
        # yet at the point the return value is computed.
        total_ssrf_validated = 0
        total_ssrf_blocked = 0

        attempt = 0
        for attempt in range(1, self.max_attempts + 1):
            # Re-checked every attempt (not just once upfront) so a
            # transient DNS hiccup gets the same retry/backoff treatment as
            # any other network failure, rather than being permanently
            # (and incorrectly) treated as "this host resolves to a blocked
            # address" - see DNSResolutionError's docstring for the real
            # false positive this caused live. DNSResolutionError must be
            # caught before SSRFBlockedError (it's a subclass): a genuine
            # blocked-IP verdict still exits immediately, unretried.
            try:
                await assert_public_url(url)
            except DNSResolutionError as exc:
                last_error = str(exc)
                last_category = "network_error"
                logger.warning("DNS resolution failed for %s (attempt %d/%d): %s", url, attempt, self.max_attempts, last_error)
                if attempt < self.max_attempts:
                    await asyncio.sleep(self.base_backoff_seconds * (2 ** (attempt - 1)))
                continue
            except SSRFBlockedError as exc:
                logger.warning("Refusing to fetch %s - blocked by SSRF guard: %s", url, exc)
                return FetchResult(url=url, ok=False, attempts=attempt, error=str(exc), blocked_ssrf=True)

            await self._throttle.wait(domain)

            context_kwargs = dict(
                user_agent=BROWSER_USER_AGENT,
                viewport=STEALTH_VIEWPORT,
                locale="en-US",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            if self._proxy_rotator is not None:
                context_kwargs["proxy"] = to_playwright_proxy(self._proxy_rotator.next())
            context = await self.browser.new_context(**context_kwargs)
            await context.add_init_script(STEALTH_INIT_SCRIPT)
            # Always installed - no toggle, no attempt skips this. Catches a
            # request whose *own* URL is a non-public address (e.g. a page's
            # link/image/script pointing directly at one) before it's ever
            # dispatched.
            guard_stats = await install_ssrf_guard(context)
            page = await context.new_page()
            pending_delay: float | None = None
            try:
                response = await page.goto(url, timeout=self.nav_timeout_ms, wait_until="domcontentloaded")
                status = response.status if response else None
                last_status = status

                # Confirmed "not there" - retrying won't change that, and
                # treating it as CANNOT VERIFY would hide a real broken link.
                if status in (404, 410):
                    logger.info("Fetch for %s got %d - confirmed broken link, not retrying", url, status)
                    return FetchResult(
                        url=url, ok=False, status=status, attempts=attempt, error=f"HTTP {status}", confirmed_not_found=True,
                        ssrf_requests_validated=total_ssrf_validated + guard_stats.validated,
                        ssrf_requests_blocked=total_ssrf_blocked + guard_stats.blocked,
                    )

                # Prefer network-idle (SPA content finished loading); if the
                # page keeps background traffic alive (analytics, polling)
                # this times out harmlessly and we fall back to the DOM we
                # already have from domcontentloaded. NOTE: this alone does
                # NOT reliably cover a JS challenge interstitial - see
                # _wait_for_challenge_to_resolve's docstring - the explicit
                # step below is what actually gives one a chance to resolve.
                try:
                    await page.wait_for_load_state("networkidle", timeout=self.settle_timeout_ms)
                except PlaywrightTimeoutError:
                    logger.debug("networkidle timed out for %s (attempt %d) - using domcontentloaded snapshot", url, attempt)

                await _dismiss_cookie_consent(page)

                html = await page.content()

                resolved_from_challenge = False
                if _looks_like_challenge(html) or _looks_like_captcha(html):
                    logger.info(
                        "Possible bot-protection interstitial/CAPTCHA detected on %s (attempt %d) - waiting up to %.0fs for it to resolve",
                        url, attempt, self.challenge_wait_seconds,
                    )
                    html = await self._wait_for_challenge_to_resolve(page, html)
                    if _looks_like_captcha(html):
                        raise _RetryableFetchFailure(
                            "A CAPTCHA challenge blocked this page and did not resolve (this tool never attempts to solve CAPTCHAs, by design)",
                            category="captcha_blocked",
                        )
                    if _looks_like_challenge(html):
                        raise _RetryableFetchFailure(
                            f"A bot-protection interstitial (e.g. a Cloudflare-style JS challenge) did not resolve after waiting {self.challenge_wait_seconds:.0f}s",
                            category="bot_blocked",
                        )
                    resolved_from_challenge = True
                    logger.info("Interstitial on %s resolved after waiting - proceeding with the resolved page", url)

                # A stale first-navigation status code (e.g. Cloudflare
                # commonly serves the challenge page itself as 503, or a
                # site returns 403 alongside a JS challenge) no longer
                # describes the page once the interstitial has genuinely
                # resolved to real content - skip the status check in that
                # case rather than fail a page we actually got through to.
                if status is not None and status >= 400 and not resolved_from_challenge:
                    if status == 429:
                        retry_after = await _parse_retry_after(response)
                        raise _RetryableFetchFailure("HTTP 429 (rate limited)", category="rate_limited", retry_after=retry_after)
                    if status in (401, 403):
                        raise _RetryableFetchFailure(f"HTTP {status} (likely blocked by bot-protection or an auth wall)", category="bot_blocked")
                    # 503 and everything else: a real HTTP error response,
                    # not a network-level failure - retried like before,
                    # under its own named category (not "unknown" - the
                    # status code itself is a specific, known reason).
                    raise _RetryableFetchFailure(f"HTTP {status}", category="http_error")

                if _looks_bodyless(html):
                    raise RuntimeError("Got a body-less document - retrying")

                # Third layer: the per-request guard validates each request's
                # *own* URL, but Chromium does not expose intermediate
                # redirect-chain hops as separately interceptable requests -
                # verified live, true for both route.continue_() and
                # route.fetch()+fulfill(). This is the only check that can
                # catch "the chain redirected somewhere bad": it inspects
                # page.url, which does correctly reflect the true final
                # address (unlike route.fetch()'s own default redirect-
                # following, which would silently resolve the chain without
                # ever updating it - why install_ssrf_guard uses
                # max_redirects=0).
                await assert_public_url(page.url)

                text = await page.inner_text("body")
                final_url = page.url
                return FetchResult(
                    url=url, ok=True, status=status, html=html, text=text,
                    final_url=final_url, attempts=attempt,
                    ssrf_requests_validated=total_ssrf_validated + guard_stats.validated,
                    ssrf_requests_blocked=total_ssrf_blocked + guard_stats.blocked,
                )
            except DNSResolutionError as exc:
                last_error = str(exc)
                last_category = "network_error"
                logger.warning("Post-navigation DNS resolution failed for %s (attempt %d/%d): %s", url, attempt, self.max_attempts, last_error)
            except SSRFBlockedError as exc:
                logger.warning("Fetch for %s landed on a blocked address after redirect: %s", url, exc)
                return FetchResult(
                    url=url, ok=False, attempts=attempt, error=str(exc), blocked_ssrf=True,
                    ssrf_requests_validated=total_ssrf_validated + guard_stats.validated,
                    ssrf_requests_blocked=total_ssrf_blocked + guard_stats.blocked,
                )
            except _RetryableFetchFailure as exc:
                last_error = str(exc)
                last_category = exc.category
                pending_delay = exc.retry_after
                if exc.category == "bot_blocked":
                    bot_block_hits += 1
                logger.warning("Fetch attempt %d/%d failed for %s (%s): %s", attempt, self.max_attempts, url, exc.category, last_error)
            except (PlaywrightTimeoutError, PlaywrightError, RuntimeError) as exc:
                last_error = str(exc)
                last_category = "network_error"
                logger.warning("Fetch attempt %d/%d failed for %s: %s", attempt, self.max_attempts, url, last_error)
            finally:
                total_ssrf_validated += guard_stats.validated
                total_ssrf_blocked += guard_stats.blocked
                await context.close()

            # A CAPTCHA won't resolve by retrying the identical request -
            # stop immediately rather than hammer it (Part 5.1). A repeated
            # bot-protection block (403/401/unresolved interstitial) is
            # capped lower than max_attempts for the same reason (Part 1.1).
            if last_category == "captcha_blocked":
                break
            if last_category == "bot_blocked" and bot_block_hits >= self.max_bot_block_attempts:
                break

            if attempt < self.max_attempts:
                if pending_delay is not None:
                    backoff = min(pending_delay, self.max_backoff_seconds)
                else:
                    backoff = min(self.base_backoff_seconds * (2 ** (attempt - 1)), self.max_backoff_seconds)
                await asyncio.sleep(backoff)

        logger.error("All fetch attempts exhausted for %s after %d attempt(s) - marking CANNOT VERIFY (%s: %s)", url, attempt, last_category, last_error)
        return FetchResult(
            url=url, ok=False, status=last_status, attempts=attempt, error=last_error,
            rate_limited=(last_category == "rate_limited"),
            likely_bot_blocked=(last_category == "bot_blocked"),
            likely_captcha_blocked=(last_category == "captcha_blocked"),
            network_error=(last_category == "network_error"),
            http_error=(last_category == "http_error"),
            # Every attempt's browser context is gone by now (each one
            # accumulated into these totals in the loop's `finally` block
            # above) - a fetch that exhausted every retry still had its
            # navigation request(s) legitimately validated before each one
            # failed to complete; that must not read as "0 requests
            # validated," which would misleadingly suggest the SSRF guard
            # never ran on this fetch at all (see SSRFGuardStats' docstring).
            ssrf_requests_validated=total_ssrf_validated,
            ssrf_requests_blocked=total_ssrf_blocked,
        )
