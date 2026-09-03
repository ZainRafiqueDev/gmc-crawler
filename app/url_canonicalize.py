"""Strips known filter/sort facet query parameters that WooCommerce (and
similar) storefronts add to collection URLs - they never change which
compliance-relevant content is on the page, only its sort/filter state, so
treating each combination as a distinct page wastes crawl budget on
near-duplicates of the same category instead of reaching more of the
catalog. Verified live: roughly half of a real 218-page crawl against a
faceted-nav store was color/size/stock/sale query-parameter variants of the
same dozen category pages.

Pagination (/page/N) is deliberately NOT collapsed here - unlike a filter
facet, page 2+ of a collection can surface products not linked from page 1,
so it still needs to be crawled for discovery. app/report.py groups
pagination pages together for *display* instead (see canonical_page_key),
without skipping the crawl.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Exact-match noise keys (WooCommerce's built-in stock/sale/featured filters)
# plus prefix families a store can extend indefinitely (one filter_<attribute>
# /query_type_<attribute> pair per filterable product attribute the store
# defines) - a fixed enum can't cover every store's custom attributes, so
# prefixes are matched instead of listed exhaustively.
_NOISE_EXACT_KEYS = {"on-sale", "in-stock", "on-backorder", "featured"}
_NOISE_PREFIXES = ("filter_", "query_type_")

_PAGINATION_RE = re.compile(r"/page/\d+/?$")


def _is_noise_query_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in _NOISE_EXACT_KEYS or any(lowered.startswith(p) for p in _NOISE_PREFIXES)


def strip_noise_query_params(url: str) -> str:
    """Removes known facet/filter/sort query parameters, leaving everything
    else (including genuinely distinguishing query params, if any) intact.
    Used at crawl-dedup time (app/site_mapper.py's _normalize) so a facet
    variant is never queued as a separate fetch in the first place - the
    existing seen-URL set does the deduplication for free once the variants
    collapse to the same string.
    """
    parsed = urlparse(url)
    kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not _is_noise_query_key(k)]
    return urlunparse(parsed._replace(query=urlencode(kept)))


def canonical_page_key(url: str) -> str:
    """The 'logical page' a URL belongs to for report-grouping purposes:
    noise query params stripped (see strip_noise_query_params) AND a
    trailing /page/N pagination segment stripped. Two URLs sharing this key
    are the same collection for reporting even though pagination pages are
    still individually crawled for discovery - see module docstring.
    """
    de_faceted = strip_noise_query_params(url)
    parsed = urlparse(de_faceted)
    depaginated_path = _PAGINATION_RE.sub("", parsed.path) or "/"
    return urlunparse(parsed._replace(path=depaginated_path))
