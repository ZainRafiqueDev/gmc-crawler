"""Opt-in residential/rotating-proxy support (Part 5.2 of the broader
real-world crawl-robustness round) - deliberately NOT built silently.

This was added only after live testing this same round found concrete
evidence of IP/browser-fingerprint-level blocking that no amount of
retry/backoff, politeness delay, cookie-consent handling, or interstitial-
waiting can get past: a real store (decathlon.fr) returned a normal HTTP
403 to a plain `curl` request but refused the TCP/TLS connection outright
for a headless-Chromium fingerprint from the same source IP; another
(tiendanimal.es) was reachable via `curl` but not via headless Chromium at
all, from the same machine. Brought into scope on the user's explicit
decision, not a default-on assumption.

Off by default: no proxy env vars set means no proxy is used anywhere,
zero behavior change from before this module existed.

This is BYO-proxy configuration only. This project does not operate,
harvest, or bundle any proxy infrastructure of its own - it lets an
operator who already has a legitimate proxy subscription (their own paid
provider - Bright Data, Oxylabs, Smartproxy, IPRoyal, or a private/self-run
proxy) point this tool at it, the same way robots.txt compliance and an
identifiable User-Agent are already built in rather than spoofed. It does
not add anything that "cracks" a site's defenses on its own; it changes
what an already-honest request looks like at the network layer.

Two supported shapes, matching how real proxy products actually work:
  - A single "rotating residential" gateway (the common case): one
    host:port + credentials, where the *provider* rotates the exit IP
    per new connection on their end. Configure PROXY_SERVER (+ PROXY_USERNAME/
    PROXY_PASSWORD if the provider issues them separately from the URL).
    Every fetch already opens a fresh Playwright browser context per
    attempt (app.fetch.PageFetcher), so this rotates "for free."
  - A pool of distinct static proxy endpoints the operator supplies
    directly: set PROXY_POOL to a comma-separated list of full proxy URLs
    (`http://user:pass@host1:port,http://user:pass@host2:port`). This
    module round-robins through them client-side. Takes precedence over
    PROXY_SERVER if both are set.

The SSRF guard (app.security.ssrf_guard) stays fully active regardless of
proxy configuration - it validates the destination hostname before any
request is dispatched, on the same transport instance that then makes the
(possibly-proxied) connection. Routing through an external proxy changes
which network the request appears to originate from; it has no bearing on
what destinations this tool is willing to fetch.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from urllib.parse import quote, urlparse

from app.config import Settings


@dataclass(frozen=True)
class ProxyConfig:
    server: str  # e.g. "http://gw.provider.com:8000" or "socks5://host:port" - no embedded credentials
    username: str | None = None
    password: str | None = None


class ProxyRotator:
    """Round-robins through a small pool of proxy configs. A single-entry
    pool (the common "one rotating-residential gateway" case) just returns
    the same config every time - correct, since no client-side rotation is
    needed for that product shape; the provider rotates server-side."""

    def __init__(self, configs: list[ProxyConfig]) -> None:
        if not configs:
            raise ValueError("ProxyRotator needs at least one ProxyConfig")
        self.configs = configs
        self._cycle = itertools.cycle(configs)

    def next(self) -> ProxyConfig:
        return next(self._cycle)


def _parse_proxy_url(raw: str) -> ProxyConfig:
    raw = raw.strip()
    parsed = urlparse(raw)
    server = f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
    return ProxyConfig(server=server, username=parsed.username, password=parsed.password)


def build_proxy_rotator(settings: Settings) -> ProxyRotator | None:
    """None when no proxy is configured (the default) - every caller must
    treat that as "use no proxy," not an error."""
    pool_raw = (settings.proxy_pool or "").strip()
    if pool_raw:
        configs = [_parse_proxy_url(u) for u in pool_raw.split(",") if u.strip()]
        if configs:
            return ProxyRotator(configs)

    if settings.proxy_server:
        return ProxyRotator([ProxyConfig(
            server=settings.proxy_server,
            username=settings.proxy_username or None,
            password=settings.proxy_password or None,
        )])

    return None


def to_playwright_proxy(cfg: ProxyConfig) -> dict:
    """Playwright's `proxy=` context/browser option: {"server": ..., optional
    "username"/"password"}."""
    proxy: dict = {"server": cfg.server}
    if cfg.username:
        proxy["username"] = cfg.username
    if cfg.password:
        proxy["password"] = cfg.password
    return proxy


def to_httpx_proxy_url(cfg: ProxyConfig) -> str:
    """httpx's `proxy=` takes a single URL with credentials embedded."""
    parsed = urlparse(cfg.server)
    auth = ""
    if cfg.username:
        auth = quote(cfg.username, safe="")
        if cfg.password:
            auth += f":{quote(cfg.password, safe='')}"
        auth += "@"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{auth}{parsed.hostname}{port}"
