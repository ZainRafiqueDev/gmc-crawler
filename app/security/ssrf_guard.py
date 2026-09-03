"""SSRF protection for every outbound fetch this project makes - both the
Playwright-driven site crawl and every httpx call (platform detection,
sitemap fetch, image probing, WooCommerce/Shopify API calls, the policy
watcher). A frontend that accepts an arbitrary user-supplied URL and fetches
it server-side is a textbook SSRF vector, so this is applied at three layers:

1. `assert_public_url` - validated once at audit-start time (fail fast with
   a clear error before doing any work).
2. `SSRFSafeTransport` (httpx) / `install_ssrf_guard` (Playwright) - re-
   validated before every individual request a context makes, not just the
   original input URL. A URL that resolves to a public IP when the user
   submits it can still resolve to an internal IP moments later (DNS
   rebinding) - checking once at input time is not sufficient.
3. A post-navigation check of the page's actual final URL (see
   app/fetch.py) - the necessary backstop for redirects specifically. httpx's
   transport (layer 2) genuinely re-invokes itself per redirect hop when
   `follow_redirects=True`, so layer 2 alone covers httpx fully. Playwright
   does not: verified live that a fulfilled 3xx response causes the browser
   to follow the Location header as an internal navigation that never
   raises a second `context.route()` event - Chromium does not expose
   intermediate redirect-chain hops as separately interceptable requests,
   for either `route.continue_()` or `route.fetch()+route.fulfill()`. That
   makes layer 3 required for Playwright fetches: layer 2 still blocks a
   request whose *own* URL is bad (a page's link/image/script pointing
   directly at a bad address - the common case, verified live against the
   real cloud metadata address), but only layer 3 catches a redirect that
   *lands* somewhere bad.

Blocked: private/loopback/link-local/reserved/multicast IP ranges (covers
127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16
including the 169.254.169.254 cloud metadata endpoint, and their IPv6
equivalents), and any scheme other than http/https.
"""
from __future__ import annotations

import asyncio
import contextvars
import ipaddress
import logging
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("gmc_audit.security.ssrf_guard")

_ALLOWED_SCHEMES = {"http", "https"}

# Used consistently across every outbound httpx/Playwright request this
# project makes (section 4.6) - identifiable, not spoofing a real browser.
GMC_AUDIT_USER_AGENT = "gmc-compliance-auditor/0.1 (+automated GMC policy compliance audit)"


class SSRFBlockedError(httpx.HTTPError):
    """Raised when a URL is refused because it (or something it redirected
    to) resolves to a non-public address, or uses a disallowed scheme.

    Subclasses httpx.HTTPError deliberately: every existing `except
    httpx.HTTPError` handler across the codebase (sitemap fetch, image
    probing, WC/Shopify API calls, the policy watcher) already treats a
    failed request as "couldn't fetch it, degrade gracefully" - an SSRF
    block should be handled exactly the same way there, with no need to
    special-case it at every call site. app/fetch.py, the one place where
    a blocked URL needs distinct handling (never retried, never reported as
    CANNOT_VERIFY), still catches SSRFBlockedError specifically - except for
    DNSResolutionError (below), which it deliberately treats differently.
    """


class DNSResolutionError(SSRFBlockedError):
    """The resolver itself failed (NXDOMAIN, timeout, a transient local/ISP
    DNS hiccup) - this is a reliability failure, not a security decision,
    and must not be conflated with "resolution succeeded but the IP is
    blocked". Subclasses SSRFBlockedError so every existing `except
    SSRFBlockedError`/`except httpx.HTTPError` call site keeps degrading
    gracefully unchanged - only app/fetch.py needs to (and does) catch this
    specifically, ahead of the SSRFBlockedError handler, to retry it like
    any other transient fetch failure instead of treating it as an
    instant, non-retried block.

    Found live: a real audit against a real store (britanniagifts.us) had a
    transient DNS hiccup partway through a 218-page crawl. Every page fetched
    after that point failed with this exact error, was marked blocked_ssrf
    (never retried) instead of cannot_verify, and three required policy
    pages that genuinely exist (shipping/returns/terms - confirmed by
    fetching them directly) were reported as "confirmed missing" instead of
    "could not be verified" - a real, user-facing false positive.
    """


def is_blocked_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable - refuse rather than guess
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


_DNS_RETRY_ATTEMPTS = 3
_DNS_RETRY_BACKOFF_SECONDS = 0.4


async def resolve_all_ips(hostname: str) -> list[str]:
    """Every A/AAAA record for hostname - SSRF checks must reject if ANY
    resolved address is non-public, not just the first one returned.

    Retries a few times on a transient resolver failure before giving up -
    this is the single choke point every assert_public_url caller goes
    through (audit.py's upfront check, monitor_service's registration
    check, the API's job-creation check, and app.fetch's per-page/per-
    request checks), so fixing it here covers all of them. Found live: a
    real audit aborted entirely at the CLI's one-shot upfront check on a
    transient DNS hiccup, on a store that resolved fine moments before and
    after - the same class of bug already fixed for the per-page crawl loop
    (see DNSResolutionError's docstring), just at a call site with no retry
    loop of its own to fall back on.
    """
    loop = asyncio.get_running_loop()
    last_error: socket.gaierror | None = None
    for attempt in range(1, _DNS_RETRY_ATTEMPTS + 1):
        try:
            infos = await loop.getaddrinfo(hostname, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
            return sorted({info[4][0] for info in infos})
        except socket.gaierror as exc:
            last_error = exc
            if attempt < _DNS_RETRY_ATTEMPTS:
                logger.warning("DNS resolution failed for %r (attempt %d/%d): %s - retrying", hostname, attempt, _DNS_RETRY_ATTEMPTS, exc)
                await asyncio.sleep(_DNS_RETRY_BACKOFF_SECONDS * attempt)
    raise DNSResolutionError(f"Could not resolve host {hostname!r}: {last_error}") from last_error


async def assert_public_url(url: str) -> None:
    """Validates scheme + resolves the host + rejects if any resolved IP is
    non-public. Call this both at audit-start (fail fast) and immediately
    before each individual fetch (see SSRFSafeTransport/install_ssrf_guard) -
    a single check at input time does not protect against DNS rebinding.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise SSRFBlockedError(f"Scheme {parsed.scheme!r} is not allowed (only http/https).")
    if not parsed.hostname:
        raise SSRFBlockedError(f"URL has no hostname: {url!r}")

    ips = await resolve_all_ips(parsed.hostname)
    blocked = [ip for ip in ips if is_blocked_ip(ip)]
    if blocked:
        raise SSRFBlockedError(f"Host {parsed.hostname!r} resolves to a blocked address: {blocked}")


class SSRFSafeTransport(httpx.AsyncHTTPTransport):
    """Drop-in replacement for httpx's default transport that re-resolves
    and re-validates the target host immediately before every request this
    transport handles - including each redirect hop, since httpx invokes
    the transport again per hop when follow_redirects=True.

    Accepts the same `proxy=` constructor argument as the base
    AsyncHTTPTransport (opt-in proxy support, app.proxy_config) - the proxy
    is configured directly on *this* transport rather than left to
    httpx.AsyncClient's own proxy-mount handling, so there is no ambiguity
    about whether a proxied request still goes through the SSRF check: it
    always does, since this transport's handle_async_request always runs
    first regardless of how the underlying connection is made.
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await assert_public_url(str(request.url))
        return await super().handle_async_request(request)


# Set once per audit run (app.graph.run_audit/run_audit_streaming) from
# Settings via app.proxy_config - a contextvar, not a plain module global,
# so concurrent audits (e.g. the FastAPI backend running several jobs at
# once) each keep their own proxy setting without racing. None (the
# default) means "no proxy" everywhere, unchanged from before this existed.
_current_proxy: contextvars.ContextVar[str | None] = contextvars.ContextVar("gmc_audit_current_proxy", default=None)


def set_current_proxy(proxy_url: str | None) -> contextvars.Token:
    """See _current_proxy's docstring. Always pair with reset_current_proxy
    in a finally block."""
    return _current_proxy.set(proxy_url)


def reset_current_proxy(token: contextvars.Token) -> None:
    _current_proxy.reset(token)


def safe_async_client(**kwargs) -> httpx.AsyncClient:
    """Use this instead of httpx.AsyncClient(...) everywhere this project
    makes an outbound request to a target-store-controlled URL. Applies the
    project's identifiable User-Agent by default (callers can still override
    per-request via the `headers=` kwarg on an individual .get()/.post() call).

    Uses the current contextvar-configured proxy (see set_current_proxy)
    unless a caller passes transport= or proxy= explicitly.
    """
    if "transport" not in kwargs:
        proxy = kwargs.pop("proxy", None) or _current_proxy.get()
        kwargs["transport"] = SSRFSafeTransport(proxy=proxy) if proxy else SSRFSafeTransport()
    headers = {"User-Agent": GMC_AUDIT_USER_AGENT}
    headers.update(kwargs.pop("headers", None) or {})
    kwargs["headers"] = headers
    return httpx.AsyncClient(**kwargs)


@dataclass
class SSRFGuardStats:
    """Positive, countable confirmation that the guard actually ran on a
    context - not just an absence of blocked requests. `validated` counts
    every request whose destination the guard checked and allowed through
    (i.e. assert_public_url passed) - regardless of what happens to the
    request afterward (it may still time out, get reset, or the page/context
    may tear down before a response arrives); `blocked` counts every one it
    refused. This is deliberately "was this destination checked and
    allowed," not "did a full round trip complete" - see install_ssrf_guard's
    docstring for the real report-honesty bug this distinction fixes.
    """

    validated: int = 0
    blocked: int = 0


async def install_ssrf_guard(context) -> SSRFGuardStats:
    """Playwright per-request guard: intercepts every request (navigation
    and subresources - scripts, XHR, images) made within this browser
    context and aborts any whose host resolves to a non-public address.
    Call once per browser context, right after creating it; always active,
    no toggle, no fallback path that skips it.

    Uses route.fetch(max_redirects=0) + route.fulfill(response=...) rather
    than the simpler route.continue_(). Both were verified to validate
    requests identically, but on a real WooCommerce store, route.continue_()
    - completely unmodified, matching almost no requests, mid-CDP-interaction
    - made the browser receive that host's chunked response truncated to a
    bodyless <head> tag; explicitly fetching and handing back the exact
    response avoided it (verified across 5 repeated live runs, plus a
    dedicated block test: a page's own fetch() to the cloud metadata address
    was aborted with zero bytes sent). max_redirects=0 specifically (not the
    route.fetch() default of 20) matters for a different reason: with the
    default, route.fetch() silently resolves the entire redirect chain
    itself and page.url never updates past the originally-requested URL -
    the post-navigation final-URL check in app/fetch.py would be validating
    the wrong address. With max_redirects=0, a 3xx response is hand back
    as-is and the browser follows it natively, so page.url ends up correct.

    `stats.validated` increments the moment assert_public_url passes -
    before route.fetch() is even attempted, not after it (and route.fulfill())
    both complete. Found live: a report on a real 20-second Playwright
    navigation timeout showed "0 request(s) validated," reading as "the SSRF
    guard never checked anything on this fetch" when what actually happened
    is the navigation request WAS validated and allowed through, then the
    connection itself just never got a response in time. Counting only a
    completed round-trip conflated "was this checked" (a security-relevant,
    already-true fact by the time the network call is attempted) with "did
    it finish" (a reliability fact, already surfaced separately via
    FetchResult's failure_category) - the two were never the same claim.
    """
    stats = SSRFGuardStats()

    async def handle_route(route) -> None:
        try:
            await assert_public_url(route.request.url)
        except SSRFBlockedError as exc:
            stats.blocked += 1
            logger.warning("Blocked SSRF attempt: %s", exc)
            await route.abort()
            return
        stats.validated += 1
        try:
            response = await route.fetch(max_redirects=0)
            await route.fulfill(response=response)
        except Exception as exc:
            # A request still in flight when the page/context is torn down
            # (a late font, a deferred script, a chat-widget embed) can fail
            # here with e.g. TargetClosedError - benign, the page's own
            # content was already captured by then. Abort rather than leave
            # it hanging or crash the whole page load.
            logger.debug(
                "SSRF-guarded fetch failed for %s (likely a late in-flight request): %s",
                route.request.url, exc,
            )
            try:
                await route.abort()
            except Exception:
                pass

    await context.route("**/*", handle_route)
    return stats
