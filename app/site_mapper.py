"""Step 2: crawl the site (homepage + sitemap.xml seeds) and classify every
internal URL found into a PageType. Depth/page-count capped via Settings.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import re
from urllib.parse import urljoin, urlparse, urlunparse
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Browser

from app.checks.woocommerce_products import fetch_wc_product_count
from app.config import Settings
from app.fetch import PageFetcher
from app.models import CrawledPage, PageType, Platform, SiteMap
from app.page_classifier import classify_page, looks_like_catalog_priority_url, looks_like_overview_priority_url
from app.proxy_config import build_proxy_rotator, to_httpx_proxy_url
from app.security.robots import RobotsChecker
from app.security.ssrf_guard import reset_current_proxy, safe_async_client, set_current_proxy
from app.url_canonicalize import strip_noise_query_params

logger = logging.getLogger("gmc_audit.site_mapper")

_SITEMAP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
_NON_CRAWLABLE_SCHEMES = ("mailto:", "tel:", "javascript:", "sms:", "whatsapp:")
_ASSET_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".pdf", ".css", ".js",
    ".woff", ".woff2", ".ttf", ".zip", ".mp4", ".ico", ".xml",
)


def _normalize(url: str) -> str:
    # Facet/filter/sort query params stripped before this becomes the seen-
    # URL dedup key (app.url_canonicalize) - a store's color/size/stock/sale
    # filter combinations of the same category page collapse onto the one
    # canonical (unfiltered) URL and are never queued as separate fetches,
    # freeing real crawl budget for genuinely distinct pages. Verified live:
    # roughly half of a real 218-page crawl was facet-query duplicates of a
    # dozen categories.
    url = strip_noise_query_params(url)
    parsed = urlparse(url)
    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", parsed.query, ""))


def _category_key_for_product_url(url: str) -> str:
    """Best-effort category grouping for a product URL with no discovering
    collection page to attribute it to (per-category page caps follow-up,
    Part 1.2) - the URL's own parent path segment, e.g.
    "/product-category/mugs/blue-mug" -> "/product-category/mugs". A flat
    URL scheme with no category segment (e.g. "/product/blue-mug") collapses
    every product onto one shared bucket - no worse than having no
    per-category cap at all for a store whose URLs don't expose category
    structure, and the crawl-graph-derived key (see _enqueue_if_allowed)
    still applies whenever a real discovering collection page is known.
    """
    path = urlparse(url).path.rstrip("/")
    parent = path.rsplit("/", 1)[0] or "/"
    return parent


def _root_netloc(netloc: str) -> str:
    return netloc.lower().removeprefix("www.")


def _is_internal(url: str, home_netloc: str) -> bool:
    return _root_netloc(urlparse(url).netloc) == home_netloc


# Tried in order; the first candidate that yields any URLs wins. sitemap.xml
# is the common SEO-plugin (Yoast/RankMath) path; wp-sitemap.xml is
# WordPress core's own default sitemap (present since WP 5.5, no plugin
# needed). Both matter: observed live, a real WooCommerce store's bot-
# mitigation blocked /sitemap.xml outright (403) while /wp-sitemap.xml was
# served normally - relying on only one candidate silently loses all sitemap-
# based crawl-priority seeding on sites like that.
_SITEMAP_CANDIDATES = ("sitemap.xml", "wp-sitemap.xml")

# Adaptive page-budget scaling (follow-up round, Part 1.1): the flat default
# (Settings.crawl_max_pages, 150) is sized for a mid-size store - too small
# for a large catalog (most of the budget goes to the first few hundred
# products, no room left for the long tail of collections/policy pages) and
# wastefully large for a small one (the crawl loop already stops once the
# wave empties, but priority_cap below is sized off crawl_max_pages, so an
# inflated budget still distorts priority seeding on a small store). Sized
# from a real signal - WooCommerce's own reported total catalog count when
# credentials are configured (most precise), else the sitemap's own
# catalog-tagged URL count, else the sitemap's total URL count, else left at
# the configured default when no signal is available at all (no sitemap).
# Only ever applied when the caller did NOT pass an explicit override - see
# Settings.crawl_max_pages_explicit in app/config.py.
_ADAPTIVE_BUDGET_FLOOR = 60  # room for homepage + policy/contact pages + nav even on a tiny store
_ADAPTIVE_BUDGET_OVERHEAD = 40  # non-catalog pages most stores have regardless of catalog size
_ADAPTIVE_BUDGET_CATALOG_MULTIPLIER = 2.0  # catalog pages are rarely the whole crawl - collections/variants/nav add on top


def _adaptive_page_budget(catalog_url_count: int, total_sitemap_url_count: int, configured_default: int) -> int:
    if catalog_url_count > 0:
        return max(_ADAPTIVE_BUDGET_FLOOR, round(catalog_url_count * _ADAPTIVE_BUDGET_CATALOG_MULTIPLIER) + _ADAPTIVE_BUDGET_OVERHEAD)
    if total_sitemap_url_count > 0:
        return max(_ADAPTIVE_BUDGET_FLOOR, total_sitemap_url_count)
    return configured_default


async def _fetch_sitemap_document(sitemap_url: str, _depth: int, _seen: set[str]) -> list[str]:
    if _depth > 2 or len(_seen) > 500 or sitemap_url in _seen:
        return []
    _seen.add(sitemap_url)

    try:
        async with safe_async_client(timeout=_SITEMAP_TIMEOUT) as client:
            resp = await client.get(sitemap_url, follow_redirects=True)
        if resp.status_code != 200:
            return []
        root = ElementTree.fromstring(resp.content)
    except (httpx.HTTPError, ElementTree.ParseError) as exc:
        logger.debug("Sitemap fetch/parse failed for %s: %s", sitemap_url, exc)
        return []

    tag = root.tag.rsplit("}", 1)[-1]
    urls: list[str] = []
    if tag == "sitemapindex":
        nested = [loc.text.strip() for loc in root.findall(".//sm:sitemap/sm:loc", _SITEMAP_NS) if loc.text]
        results = await asyncio.gather(*(_fetch_sitemap_document(n, _depth + 1, _seen) for n in nested))
        for r in results:
            urls.extend(r)
    elif tag == "urlset":
        urls = [loc.text.strip() for loc in root.findall(".//sm:url/sm:loc", _SITEMAP_NS) if loc.text]

    return urls


async def _fetch_sitemap_urls(base_url: str) -> list[str]:
    for candidate in _SITEMAP_CANDIDATES:
        urls = await _fetch_sitemap_document(urljoin(base_url + "/", candidate), 0, set())
        if urls:
            return urls
    return []


def _is_asset_link(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(_ASSET_EXTENSIONS)


# WooCommerce (and similar) "quick add" links that mutate a session's cart
# via a plain GET - visiting one is a real side effect (adds an item to
# whatever cart the crawler's session happens to have), not just an
# expensive no-op. Site owners commonly Disallow these explicitly in
# robots.txt; skip them unconditionally rather than depend on robots.txt
# having been fetchable (some hosts' bot-mitigation blocks robots.txt
# itself - observed live - which would otherwise leave this unenforced).
_CART_ACTION_QUERY_KEYS = ("add-to-cart", "remove_item", "undo_item")


def _is_cart_action_link(url: str) -> bool:
    query = urlparse(url).query.lower()
    return any(f"{key}=" in query for key in _CART_ACTION_QUERY_KEYS)


def _extract_links_and_images(html: str, page_url: str) -> tuple[list[str], list[str]]:
    soup = BeautifulSoup(html, "lxml")
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith(_NON_CRAWLABLE_SCHEMES):
            continue
        absolute = urljoin(page_url, href)
        if absolute.startswith(("http://", "https://")) and not _is_asset_link(absolute) and not _is_cart_action_link(absolute):
            links.append(absolute)

    images = []
    for img in soup.find_all("img", src=True):
        images.append(urljoin(page_url, img["src"].strip()))

    return links, images


_BOILERPLATE_SELECTORS = ("nav", "header", "footer", "script", "style")
_BOILERPLATE_ATTR_PATTERN = re.compile(r"cookie|consent|gdpr|newsletter", re.IGNORECASE)


def _main_content_text(soup: BeautifulSoup, limit: int = 3000) -> str:
    """Text used for classification only - strips nav/header/footer/cookie-
    consent boilerplate so a "see our Privacy Policy" cookie banner (present
    on nearly every page) can't make every page look like the privacy page.
    """
    for tag_name in _BOILERPLATE_SELECTORS:
        for el in soup.find_all(tag_name):
            el.decompose()
    for el in soup.find_all(attrs={"class": _BOILERPLATE_ATTR_PATTERN}):
        el.decompose()
    for el in soup.find_all(attrs={"id": _BOILERPLATE_ATTR_PATTERN}):
        el.decompose()
    main = soup.find("main") or soup.body or soup
    return main.get_text(separator=" ", strip=True)[:limit]


def _extract_lang(soup: BeautifulSoup) -> str | None:
    """Best-effort page content language from <html lang="...">, normalized
    to a base 2-letter code (e.g. "es" from "es-MX"). Used to avoid a
    confident "missing page" verdict on a store whose content the
    classifier's English/known-language patterns can't recognize (see
    app.page_classifier.SUPPORTED_LANGUAGES, app.checks.deterministic)."""
    if not soup.html:
        return None
    lang = soup.html.get("lang")
    if not lang or not isinstance(lang, str):
        return None
    base = lang.strip().split("-")[0].lower()
    return base or None


async def _fetch_and_classify(fetcher: PageFetcher, url: str, depth: int, home_netloc: str, is_homepage: bool) -> CrawledPage:
    result = await fetcher.fetch(url)
    path = urlparse(url).path or "/"

    if not result.ok:
        return CrawledPage(
            url=url,
            page_type=classify_page(url, path, is_homepage, [], ""),
            depth=depth,
            status=result.status,
            reachable=False,
            cannot_verify=result.cannot_verify,
            error=result.error,
            failure_category=result.failure_category,
            # Was dropped here even after app.fetch.PageFetcher started
            # correctly reporting a nonzero count on a failed fetch (the
            # guard validates a request's destination before attempting it,
            # not only after a full round trip completes - see
            # SSRFGuardStats' docstring) - this was the actual reason a
            # real report on a timed-out page still showed "0 request(s)
            # validated": the value existed on `result` but was never
            # copied onto the CrawledPage the report reads from.
            ssrf_requests_validated=result.ssrf_requests_validated,
            ssrf_requests_blocked=result.ssrf_requests_blocked,
        )

    soup = BeautifulSoup(result.html or "", "lxml")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    headings = [h.get_text(strip=True) for h in soup.find_all(["h1", "h2"]) if h.get_text(strip=True)]
    links, images = _extract_links_and_images(result.html or "", result.final_url or url)
    detected_language = _extract_lang(soup)

    internal_links = sorted({l for l in links if _is_internal(l, home_netloc)})
    external_links = sorted({l for l in links if not _is_internal(l, home_netloc)})

    # De-boilerplated main content: used for classification (3000 chars is
    # plenty there) AND reused as-is for LLM-graded check prompts (section
    # 2.1 of the hardening round), which want more context - one strip pass,
    # sliced twice, rather than re-walking the tree for each purpose.
    main_text = _main_content_text(soup, limit=8000)
    classification_headings = ([title] if title else []) + headings
    page_type = classify_page(url, path, is_homepage, classification_headings, main_text[:3000])

    return CrawledPage(
        url=url,
        page_type=page_type,
        depth=depth,
        title=title,
        headings=headings,
        status=result.status,
        reachable=True,
        cannot_verify=False,
        internal_links=internal_links,
        external_links=external_links,
        image_srcs=images,
        html=result.html,
        text=result.text,
        main_content_text=main_text,
        ssrf_requests_validated=result.ssrf_requests_validated,
        ssrf_requests_blocked=result.ssrf_requests_blocked,
        detected_language=detected_language,
    )


async def map_site(base_url: str, browser: Browser, settings: Settings, platform: Platform | None = None) -> SiteMap:
    # Opt-in BYO proxy support (Part 5.2, app.proxy_config) - None (the
    # default, no proxy env vars set) means nothing here changes at all.
    # Set for the crawl's own httpx calls (sitemap fetch below) via the
    # contextvar every safe_async_client() picks up automatically; the
    # Playwright side gets the same rotator passed into PageFetcher
    # directly. Set/reset here so map_site is correct standalone too, not
    # only when called from within run_audit's own broader scope.
    proxy_rotator = build_proxy_rotator(settings)
    proxy_token = set_current_proxy(to_httpx_proxy_url(proxy_rotator.next()) if proxy_rotator else None)
    try:
        return await _map_site(base_url, browser, settings, proxy_rotator, platform)
    finally:
        reset_current_proxy(proxy_token)


async def _map_site(base_url: str, browser: Browser, settings: Settings, proxy_rotator, platform: Platform | None = None) -> SiteMap:
    fetcher = PageFetcher(
        browser, max_attempts=3, domain_min_delay_seconds=settings.crawl_domain_min_delay_seconds,
        proxy_rotator=proxy_rotator, extra_headers=settings.crawl_extra_headers_dict or None,
        challenge_wait_seconds=settings.crawl_challenge_wait_seconds,
    )
    home_norm = _normalize(base_url)
    home_netloc = _root_netloc(urlparse(home_norm).netloc)

    robots = RobotsChecker(home_norm)
    await robots.load()

    # Sitemap discovery and the WooCommerce product-count probe are both
    # cheap, independent reads - run concurrently so the more precise
    # platform-API signal (when credentials are configured) doesn't add its
    # own round-trip on top of the sitemap fetch.
    wc_count_task = (
        fetch_wc_product_count(base_url, settings.wc_consumer_key, settings.wc_consumer_secret)
        if platform == Platform.WOOCOMMERCE and settings.wc_consumer_key and settings.wc_consumer_secret
        else None
    )
    if wc_count_task is not None:
        sitemap_urls, wc_product_count = await asyncio.gather(_fetch_sitemap_urls(base_url), wc_count_task)
    else:
        sitemap_urls, wc_product_count = await _fetch_sitemap_urls(base_url), None
    logger.info("Sitemap discovery found %d URL(s)", len(sitemap_urls))

    if not robots.is_allowed(home_norm):
        logger.warning("robots.txt disallows the homepage (%s) - nothing to crawl", home_norm)
        return SiteMap(base_url=home_norm, pages=[], sitemap_urls_found=len(sitemap_urls), robots_disallowed=True)

    seen: set[str] = {home_norm}
    pages: list[CrawledPage] = []

    sem = asyncio.Semaphore(settings.crawl_concurrency)

    async def bounded_fetch(url: str, depth: int, is_homepage: bool) -> CrawledPage:
        async with sem:
            return await _fetch_and_classify(fetcher, url, depth, home_netloc, is_homepage)

    # Per-category page caps (follow-up round, Part 1.2): keyed by the
    # discovering category-listing page's own normalized URL when a product
    # URL was actually found via crawling a COLLECTION page's links (real
    # crawl-graph context - the strongest signal), else derived from the
    # product URL's own parent path segment for URLs seeded directly from
    # the sitemap (no discovering page to attribute them to - see
    # _category_key_for_product_url). Counts enqueue decisions, not fetches,
    # so budget is never spent queuing more of a category than will be used.
    product_counts_per_category: dict[str, int] = {}

    def _enqueue_if_allowed(url: str, depth: int, category_key: str | None, next_wave: list[tuple[str, int, str | None]]) -> None:
        if url in seen:
            return
        seen.add(url)
        if not robots.is_allowed(url):
            logger.debug("robots.txt disallows %s - skipping", url)
            return
        path = urlparse(url).path
        # Only product-looking URLs are ever capped - category-listing pages
        # (COLLECTION) are cheap and structurally important, so every one
        # discovered gets crawled regardless of this cap (still bounded by
        # the overall crawl_max_pages ceiling, same as everything else).
        if looks_like_catalog_priority_url(path):
            effective_key = category_key or _category_key_for_product_url(url)
            count = product_counts_per_category.get(effective_key, 0)
            if count >= settings.crawl_max_product_pages_per_category:
                logger.debug(
                    "Per-category product page cap (%d) reached for %r - skipping %s",
                    settings.crawl_max_product_pages_per_category, effective_key, url,
                )
                return
            product_counts_per_category[effective_key] = count + 1
        next_wave.append((url, depth, category_key))

    # Crawl prioritization (hardening round, section 3.3; two-tiered per the
    # Store-Overview-first restructuring): seed Store Overview (policy/
    # contact) sitemap URLs into the very first wave ahead of everything
    # else, then Catalog (product) URLs, then homepage-discovered nav links,
    # then everything remaining (collections, blog, etc.) - otherwise a
    # large-catalog store's dozens of top-level collection pages can consume
    # the entire page budget before the crawl ever reaches a policy or
    # product page. Reserve at most a third of the budget (min 10) across
    # both priority tiers combined so homepage nav discovery still gets a
    # meaningful share.
    overview_sitemap_urls: list[str] = []
    catalog_sitemap_urls: list[str] = []
    other_sitemap_urls: list[str] = []
    for u in sitemap_urls:
        nl = _normalize(u)
        if not _is_internal(nl, home_netloc):
            continue
        path = urlparse(nl).path
        if looks_like_overview_priority_url(path):
            overview_sitemap_urls.append(nl)
        elif looks_like_catalog_priority_url(path):
            catalog_sitemap_urls.append(nl)
        else:
            other_sitemap_urls.append(nl)

    if not settings.crawl_max_pages_explicit:
        catalog_signal = wc_product_count if wc_product_count is not None else len(catalog_sitemap_urls)
        adaptive_budget = _adaptive_page_budget(catalog_signal, len(sitemap_urls), settings.crawl_max_pages)
        if adaptive_budget != settings.crawl_max_pages:
            logger.info(
                "Adaptive page budget: %s -> sizing crawl_max_pages to %d (was %d)",
                f"{wc_product_count} WooCommerce product(s) reported" if wc_product_count is not None else f"{len(catalog_sitemap_urls)} catalog URL(s) in sitemap",
                adaptive_budget, settings.crawl_max_pages,
            )
            settings.crawl_max_pages = adaptive_budget  # clamped to HARD_MAX_PAGES by the field_validator on assignment

    priority_cap = max(10, settings.crawl_max_pages // 3)
    wave: list[tuple[str, int, str | None]] = [(home_norm, 0, None)]
    for nl in overview_sitemap_urls[:priority_cap]:
        _enqueue_if_allowed(nl, 1, None, wave)
    remaining_cap = max(0, priority_cap - len(overview_sitemap_urls))
    for nl in catalog_sitemap_urls[:remaining_cap]:
        _enqueue_if_allowed(nl, 1, None, wave)
    if overview_sitemap_urls or catalog_sitemap_urls:
        logger.info(
            "Seeded %d Store Overview + %d Catalog URL(s) from sitemap.xml ahead of nav discovery",
            min(len(overview_sitemap_urls), priority_cap), min(len(catalog_sitemap_urls), remaining_cap),
        )

    sitemap_injected = False

    while wave and len(pages) < settings.crawl_max_pages:
        room = settings.crawl_max_pages - len(pages)
        wave = wave[:room]

        batch = await asyncio.gather(*(bounded_fetch(url, depth, url == home_norm) for url, depth, _cat in wave))
        pages.extend(batch)

        next_depth = (wave[0][1] + 1) if wave else 1
        next_wave: list[tuple[str, int, str | None]] = []
        # Round-robin across this batch's pages (1st child of every page,
        # then 2nd child of every page, ...) rather than appending each
        # page's full child list before moving to the next page. A later
        # truncation (wave[:room], next loop iteration, when the combined
        # next_wave exceeds the remaining page budget) always cuts from the
        # front - sequential appending meant whichever page happened to be
        # first in this batch got its entire child list through before any
        # other page got a single child in, regardless of per-category caps
        # (which only govern *whether* a URL is enqueued at all, not where
        # it lands in next_wave). Found live on a real 111-category store:
        # one category (of ~111) supplied 10 of the only 26 product pages
        # the whole crawl found, while roughly 100 others contributed zero -
        # not because any category was anywhere near its own cap, but
        # because its collection page simply wasn't first in this loop.
        per_page_children: list[list[tuple[str, int, str | None]]] = []
        for (_url, _depth, cat_key), page in zip(wave, batch):
            if page.depth >= settings.crawl_max_depth:
                continue
            # A product URL discovered via a COLLECTION page's own links is
            # keyed to that collection - real crawl-graph context, the
            # strongest available signal. Anything discovered via a non-
            # collection page (homepage nav, another product page, etc.)
            # inherits whatever category context its own discovering page
            # had, if any - not reset to None, so e.g. a product page's
            # "related products" links stay attributed to the same category
            # rather than escaping the cap entirely.
            child_category_key = _normalize(page.url) if page.page_type == PageType.COLLECTION else cat_key
            per_page_children.append([(link, page.depth + 1, child_category_key) for link in page.internal_links])
        for round_ in itertools.zip_longest(*per_page_children):
            for entry in round_:
                if entry is None:
                    continue
                link, child_depth, child_category_key = entry
                _enqueue_if_allowed(_normalize(link), child_depth, child_category_key, next_wave)

        if not sitemap_injected:
            sitemap_injected = True
            # Any priority URLs beyond the cap, plus everything else (collections,
            # blog, etc.) - already-seen ones (including the priority slice just
            # seeded above) are naturally skipped by _enqueue_if_allowed.
            leftover = overview_sitemap_urls[priority_cap:] + catalog_sitemap_urls[remaining_cap:] + other_sitemap_urls
            for nl in leftover:
                _enqueue_if_allowed(nl, next_depth, None, next_wave)

        wave = next_wave

    logger.info("Crawl finished: %d page(s) visited (cap=%d, depth cap=%d)", len(pages), settings.crawl_max_pages, settings.crawl_max_depth)
    return SiteMap(base_url=home_norm, pages=pages, sitemap_urls_found=len(sitemap_urls))
