"""Detect-and-credit for returns/contact info hosted off the audited domain
(work-order follow-up: "we didn't crawl it" must not be scored as "it isn't
there"). Confirmed live on gymshark.com: a large, well-run store's entire
support/returns experience lives on a subdomain (support.gymshark.com) and a
third-party returns portal (us-gymshark.loopreturns.com) - the crawler
correctly never leaves the audited hostname (the same SSRF boundary that
makes this tool safe to run against arbitrary sites), but that meant
missing_returns_page/missing_contact_info fired CRITICAL on a store that
plainly has both.

This module does NOT extend the crawl to subdomains or fetch anything new -
it only scans CrawledPage.external_links, which app.site_mapper already
collects for every reachable page during the normal crawl. It can only match
on the URL text itself (no anchor/link text is captured anywhere in this
project's crawl data today) - documented as a real, accepted limitation, not
silently assumed away.

Crediting presence this way confirms a returns/contact resource is LINKED,
not that its content is compliant - the evidence this module returns always
says so explicitly, so a "pass" here is never mistaken for an audited page.
"""
from __future__ import annotations

from urllib.parse import urlparse

import tldextract

from app.models import SiteMap

# Known third-party returns-management portals - an easily-extendable list,
# not exhaustive. Matched by registrable domain, not full hostname (so
# us-gymshark.loopreturns.com and app.loopreturns.com both match
# "loopreturns.com").
RETURNS_PORTAL_DOMAINS: set[str] = {
    "loopreturns.com", "narvar.com", "happyreturns.com", "returnly.com",
    "aftership.com", "returngo.ai", "returbo.com", "returnscenter.com",
}

_RETURNS_URL_KEYWORDS = ("return", "refund", "exchange")
_CONTACT_URL_KEYWORDS = ("contact", "support", "help")


def _registrable_domain(hostname: str) -> str:
    ext = tldextract.extract(hostname)
    return f"{ext.domain}.{ext.suffix}" if ext.suffix else ext.domain


def _find_credit(site_map: SiteMap, url_keywords: tuple[str, ...], portal_domains: set[str] | None = None) -> dict | None:
    base_hostname = urlparse(site_map.base_url).hostname or ""
    base_registrable = _registrable_domain(base_hostname)

    for page in site_map.pages:
        if not page.reachable:
            continue
        for link in page.external_links:
            hostname = urlparse(link).hostname or ""
            if not hostname:
                continue

            is_known_portal = portal_domains is not None and _registrable_domain(hostname) in portal_domains
            is_same_org_subdomain = _registrable_domain(hostname) == base_registrable and hostname != base_hostname
            matches_keyword = any(k in link.lower() for k in url_keywords)

            if is_known_portal or (is_same_org_subdomain and matches_keyword):
                return {
                    "credited_url": link,
                    "found_on_page": page.url,
                    "matched_via": "known_returns_portal" if is_known_portal else "same_organization_subdomain",
                }
    return None


def find_returns_credit(site_map: SiteMap) -> dict | None:
    """A linked same-organization subdomain whose URL suggests returns/
    refunds/exchanges, OR a known third-party returns-portal domain
    (Loop, Narvar, Happy Returns, Returnly, ...). None if neither is found -
    callers must not treat that as "confirmed absent" either, only as "no
    off-domain credit available."""
    return _find_credit(site_map, _RETURNS_URL_KEYWORDS, RETURNS_PORTAL_DOMAINS)


def find_contact_credit(site_map: SiteMap) -> dict | None:
    """A linked same-organization subdomain whose URL suggests contact/
    support/help. No third-party-provider domain list for contact
    specifically - not asked for, and a generic "help desk" provider list
    (Zendesk, Intercom, ...) would be a different, broader claim than what
    was scoped here."""
    return _find_credit(site_map, _CONTACT_URL_KEYWORDS, portal_domains=None)
