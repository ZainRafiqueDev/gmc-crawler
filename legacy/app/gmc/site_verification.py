"""Injects the Google site-verification meta tag into the store's <head>.

WooCommerce goes through the WP REST API / a small custom plugin endpoint;
the future Shopify path would use a ScriptTag or edit `settings_data.json`.
Both implement the same interface so `connect.inject_with_verification`
never needs to know which platform it's talking to.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx

logger = logging.getLogger("gmc_compliance.gmc.site_verification")


class SiteVerificationInjector(ABC):
    @abstractmethod
    async def inject(self, token: str) -> bool:
        """Ask the store platform to add the verification meta tag. Returns
        whether the platform-side call itself succeeded (not proof the tag
        is live - call `fetch_page_html` to confirm that)."""
        raise NotImplementedError

    @abstractmethod
    async def fetch_page_html(self) -> str:
        raise NotImplementedError


def verification_meta_tag(token: str) -> str:
    return f'<meta name="google-site-verification" content="{token}" />'


class MockSiteVerificationInjector(SiteVerificationInjector):
    """Test/dev double. `fail_times` lets tests simulate N failed attempts
    before the injection starts succeeding, to exercise the retry path."""

    def __init__(self, fail_times: int = 0) -> None:
        self._fail_times = fail_times
        self._attempts = 0
        self._html = "<html><head></head><body>Mock store homepage</body></html>"

    async def inject(self, token: str) -> bool:
        self._attempts += 1
        if self._attempts <= self._fail_times:
            return False
        self._html = self._html.replace("</head>", f"{verification_meta_tag(token)}</head>")
        return True

    async def fetch_page_html(self) -> str:
        return self._html


class WooCommerceSiteVerificationInjector(SiteVerificationInjector):
    """Calls a small custom WP REST endpoint (e.g. a companion plugin)
    that writes the meta tag into `wp_head`, then re-fetches the homepage
    to confirm it landed."""

    def __init__(self, store_url: str, api_key: str, api_secret: str, timeout_s: float = 15.0) -> None:
        self._store_url = store_url.rstrip("/")
        self._auth = (api_key, api_secret)
        self._timeout_s = timeout_s

    async def inject(self, token: str) -> bool:
        url = f"{self._store_url}/wp-json/gmc-compliance/v1/site-verification"
        try:
            async with httpx.AsyncClient(auth=self._auth, timeout=self._timeout_s) as client:
                resp = await client.post(url, json={"token": token})
                resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            logger.warning("Site verification injection call failed: %s", exc)
            return False

    async def fetch_page_html(self) -> str:
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.get(self._store_url)
            resp.raise_for_status()
        return resp.text
