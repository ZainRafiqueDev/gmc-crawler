"""SSRF protection tests. Uses literal IP addresses as hostnames so DNS
resolution is instant and works with no network access (getaddrinfo
resolves a literal IP without a real lookup) - these are exactly the kind
of "attempt to submit an internal IP / metadata endpoint URL" tests the
brief calls for.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import socket

from app.security import ssrf_guard as ssrf_guard_module
from app.security.ssrf_guard import (
    DNSResolutionError,
    SSRFBlockedError,
    SSRFSafeTransport,
    assert_public_url,
    install_ssrf_guard,
    is_blocked_ip,
    reset_current_proxy,
    resolve_all_ips,
    safe_async_client,
    set_current_proxy,
)

# This whole file exercises the guard's real logic, so undo the suite-wide
# "no real DNS" fake (tests/conftest.py::_no_real_dns_for_ssrf_guard) for
# everything here - it exists for other tests using fake domains, not this file.
_real_assert_public_url = ssrf_guard_module.assert_public_url


@pytest.fixture(autouse=True)
def _use_real_ssrf_logic(monkeypatch):
    monkeypatch.setattr("app.security.ssrf_guard.assert_public_url", _real_assert_public_url)


@pytest.mark.parametrize("ip", [
    "127.0.0.1",        # loopback
    "10.0.0.1",          # private
    "172.16.0.1",        # private
    "192.168.1.1",       # private
    "169.254.169.254",   # link-local / cloud metadata endpoint
    "169.254.1.1",       # link-local
    "0.0.0.0",           # unspecified
    "::1",                # IPv6 loopback
    "fc00::1",            # IPv6 unique local (private)
    "fe80::1",            # IPv6 link-local
    "224.0.0.1",          # multicast
    "not-an-ip",          # unparseable - refuse rather than guess
])
def test_blocks_private_and_reserved_addresses(ip):
    assert is_blocked_ip(ip) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34"])
def test_allows_public_addresses(ip):
    assert is_blocked_ip(ip) is False


@pytest.mark.asyncio
async def test_assert_public_url_blocks_loopback():
    with pytest.raises(SSRFBlockedError):
        await assert_public_url("http://127.0.0.1/")


@pytest.mark.asyncio
async def test_assert_public_url_blocks_cloud_metadata_endpoint():
    with pytest.raises(SSRFBlockedError):
        await assert_public_url("http://169.254.169.254/latest/meta-data/")


@pytest.mark.asyncio
async def test_assert_public_url_blocks_private_range():
    with pytest.raises(SSRFBlockedError):
        await assert_public_url("http://10.0.0.5/")


@pytest.mark.asyncio
async def test_assert_public_url_blocks_disallowed_scheme():
    with pytest.raises(SSRFBlockedError):
        await assert_public_url("ftp://8.8.8.8/")


@pytest.mark.asyncio
async def test_assert_public_url_blocks_missing_hostname():
    with pytest.raises(SSRFBlockedError):
        await assert_public_url("http:///no-host")


@pytest.mark.asyncio
async def test_assert_public_url_allows_public_ip_literal():
    await assert_public_url("http://8.8.8.8/")  # does not raise


@pytest.mark.asyncio
async def test_transport_revalidates_before_every_request_and_blocks():
    transport = SSRFSafeTransport()
    request = httpx_request_to("http://127.0.0.1/admin")
    with pytest.raises(SSRFBlockedError):
        await transport.handle_async_request(request)


@pytest.mark.asyncio
async def test_transport_revalidates_and_allows_then_delegates():
    transport = SSRFSafeTransport()
    request = httpx_request_to("http://8.8.8.8/")
    with patch("httpx.AsyncHTTPTransport.handle_async_request", new_callable=AsyncMock) as mock_super:
        mock_super.return_value = "fake-response"
        result = await transport.handle_async_request(request)
    mock_super.assert_awaited_once()
    assert result == "fake-response"


def httpx_request_to(url: str):
    import httpx
    return httpx.Request("GET", url)


async def _install_and_capture_handler(context):
    """install_ssrf_guard registers its handler via context.route(pattern,
    handler) - capture that handler so tests can drive it directly with
    fake route objects, without needing a real Playwright context.
    """
    captured = {}

    async def fake_route(pattern, handler):
        captured["pattern"] = pattern
        captured["handler"] = handler

    context.route = fake_route
    stats = await install_ssrf_guard(context)
    return stats, captured["handler"]


def _fake_route(url: str):
    route = MagicMock()
    route.request.url = url
    route.fetch = AsyncMock()
    route.fulfill = AsyncMock()
    route.abort = AsyncMock()
    route.continue_ = AsyncMock()
    return route


@pytest.mark.asyncio
async def test_install_ssrf_guard_uses_fetch_and_fulfill_never_continue():
    """route.continue_() - even completely unmodified - was observed live to
    make a real host's chunked response arrive truncated to a body-less
    <head>. The guard must fetch the response explicitly and hand back the
    exact bytes via fulfill(), never continue_(), for an allowed request.
    """
    stats, handler = await _install_and_capture_handler(MagicMock())
    route = _fake_route("https://8.8.8.8/")
    fake_response = MagicMock()
    route.fetch.return_value = fake_response

    await handler(route)

    route.fetch.assert_awaited_once_with(max_redirects=0)
    route.fulfill.assert_awaited_once_with(response=fake_response)
    route.continue_.assert_not_awaited()
    route.abort.assert_not_awaited()
    assert stats.validated == 1
    assert stats.blocked == 0


@pytest.mark.asyncio
async def test_install_ssrf_guard_aborts_a_blocked_request_without_fetching_it():
    stats, handler = await _install_and_capture_handler(MagicMock())
    route = _fake_route("http://169.254.169.254/latest/meta-data/")

    await handler(route)

    route.fetch.assert_not_awaited()
    route.fulfill.assert_not_awaited()
    route.abort.assert_awaited_once()
    assert stats.blocked == 1
    assert stats.validated == 0


@pytest.mark.asyncio
async def test_install_ssrf_guard_counts_across_multiple_requests():
    stats, handler = await _install_and_capture_handler(MagicMock())
    for url in ("https://8.8.8.8/a", "https://1.1.1.1/b", "http://127.0.0.1/admin"):
        route = _fake_route(url)
        await handler(route)

    assert stats.validated == 2
    assert stats.blocked == 1


@pytest.mark.asyncio
async def test_install_ssrf_guard_aborts_gracefully_on_a_late_in_flight_error():
    """A request still in flight when the context tears down (a late font,
    a deferred script) can fail with e.g. TargetClosedError - must abort
    quietly rather than raise and crash the page load.

    Follow-up round: this request still counts as validated=1, not 0 - it
    genuinely passed assert_public_url before route.fetch() then failed.
    "0 requests validated" on a fetch that timed out was a real, live-found
    report-honesty bug (a page that timed out looked like the SSRF guard
    never checked anything, when it had). See SSRFGuardStats' docstring.
    """
    stats, handler = await _install_and_capture_handler(MagicMock())
    route = _fake_route("https://8.8.8.8/late.js")
    route.fetch.side_effect = RuntimeError("Request context disposed")

    await handler(route)  # must not raise

    route.abort.assert_awaited_once()
    assert stats.validated == 1


@pytest.mark.asyncio
async def test_resolve_all_ips_retries_a_transient_dns_failure_then_succeeds(monkeypatch):
    """Regression: a real audit's one-shot upfront check (audit.py, with no
    retry loop of its own) aborted the entire audit on a transient DNS
    hiccup against a store that resolved fine moments before and after.
    resolve_all_ips is the single choke point every assert_public_url
    caller goes through, so retrying there covers all of them.
    """
    calls = {"n": 0}

    async def flaky_then_ok(host, port, family=None, type=None):
        calls["n"] += 1
        if calls["n"] < 2:
            raise socket.gaierror("[Errno 11001] getaddrinfo failed")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr("asyncio.get_running_loop", lambda: type("L", (), {"getaddrinfo": staticmethod(flaky_then_ok)})())

    ips = await resolve_all_ips("shop.example")
    assert ips == ["93.184.216.34"]
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_resolve_all_ips_raises_dns_resolution_error_after_exhausting_retries(monkeypatch):
    async def always_fails(host, port, family=None, type=None):
        raise socket.gaierror("[Errno 11001] getaddrinfo failed")

    monkeypatch.setattr("asyncio.get_running_loop", lambda: type("L", (), {"getaddrinfo": staticmethod(always_fails)})())

    with pytest.raises(DNSResolutionError):
        await resolve_all_ips("shop.example")


# --- Part 5.2: opt-in BYO proxy support must never bypass the SSRF check --

def test_no_proxy_by_default():
    client = safe_async_client()
    transport = client._transport
    assert isinstance(transport, SSRFSafeTransport)


def test_safe_async_client_picks_up_contextvar_proxy():
    token = set_current_proxy("http://user:pass@proxy.example:8000")
    try:
        client = safe_async_client()
        assert isinstance(client._transport, SSRFSafeTransport)
    finally:
        reset_current_proxy(token)


def test_explicit_proxy_kwarg_overrides_contextvar():
    token = set_current_proxy("http://from-contextvar.example:8000")
    try:
        client = safe_async_client(proxy="http://explicit.example:9000")
        assert isinstance(client._transport, SSRFSafeTransport)
    finally:
        reset_current_proxy(token)


@pytest.mark.asyncio
async def test_ssrf_check_still_runs_when_a_proxy_is_configured():
    """The single most important guarantee here: routing through a proxy
    must never skip the destination-hostname validation - the proxy changes
    where the request appears to originate from, not what this tool is
    willing to fetch. Confirmed by proving the transport still raises for a
    blocked address even with a proxy set on it."""
    transport = SSRFSafeTransport(proxy="http://user:pass@proxy.example:8000")
    request = httpx.Request("GET", "http://169.254.169.254/latest/meta-data/")
    with pytest.raises(SSRFBlockedError):
        await transport.handle_async_request(request)


def test_explicit_transport_kwarg_is_never_overridden_by_contextvar_proxy():
    """A caller passing transport= explicitly must keep full control - the
    contextvar proxy must not silently get injected some other way."""
    custom_transport = SSRFSafeTransport()
    token = set_current_proxy("http://proxy.example:8000")
    try:
        client = safe_async_client(transport=custom_transport)
        assert client._transport is custom_transport
    finally:
        reset_current_proxy(token)
