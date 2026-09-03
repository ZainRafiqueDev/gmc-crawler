"""robots.txt compliance (section 4.6) - fetched once per crawl and
consulted before every URL is enqueued, so the crawler never visits a page
a site has explicitly disallowed for crawlers. Fails open (allows) if
robots.txt is missing/unreachable, since that's the standard, expected
default when a site simply doesn't publish one.
"""
from __future__ import annotations

import logging
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

from app.security.ssrf_guard import GMC_AUDIT_USER_AGENT, safe_async_client

logger = logging.getLogger("gmc_audit.security.robots")


class RobotsChecker:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._parser: RobotFileParser | None = None

    async def load(self) -> None:
        robots_url = urljoin(self.base_url.rstrip("/") + "/", "robots.txt")
        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            async with safe_async_client(timeout=10.0) as client:
                resp = await client.get(robots_url, headers={"User-Agent": GMC_AUDIT_USER_AGENT}, follow_redirects=True)
            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
                logger.info("Loaded robots.txt from %s", robots_url)
            else:
                parser.parse([])  # no robots.txt published - allow all
        except Exception as exc:  # noqa: BLE001 - robots.txt fetch failure must never block the audit
            logger.debug("robots.txt fetch failed for %s (%s) - defaulting to allow", robots_url, exc)
            parser.parse([])
        self._parser = parser

    def is_allowed(self, url: str) -> bool:
        if self._parser is None:
            return True  # load() wasn't called or hasn't completed - fail open
        return self._parser.can_fetch(GMC_AUDIT_USER_AGENT, url)
