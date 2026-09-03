"""Fetches GMC Help Center policy pages.

`PlaywrightPolicyPageFetcher` is the live implementation (headless browser,
handles JS-rendered content). Tests use `FixedPolicyPageFetcher` with saved
fixture HTML instead - no browser, no live network call, and it exercises
the exact same downstream hash/diff/extraction code path.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class PolicyPageFetcher(ABC):
    @abstractmethod
    async def fetch(self, url: str) -> str:
        raise NotImplementedError


class PlaywrightPolicyPageFetcher(PolicyPageFetcher):
    async def fetch(self, url: str) -> str:
        from playwright.async_api import async_playwright  # local import: optional/heavy dependency

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.goto(url, wait_until="networkidle")
                return await page.content()
            finally:
                await browser.close()


class FixedPolicyPageFetcher(PolicyPageFetcher):
    """Returns pre-loaded HTML per URL. For tests/dev - no browser, no network."""

    def __init__(self, pages: dict[str, str]) -> None:
        self._pages = pages

    async def fetch(self, url: str) -> str:
        return self._pages[url]

    def set_page(self, url: str, html: str) -> None:
        self._pages[url] = html
