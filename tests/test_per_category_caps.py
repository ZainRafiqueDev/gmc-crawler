"""Per-category page caps (follow-up round, Part 1.2). _category_key_for_product_url
is a pure function - covered directly. The wiring test confirms a many-category
store (products discovered via real COLLECTION-page crawl-graph links, not URL
guessing) actually gets capped per category rather than one flat catalog total,
and that category-listing pages themselves are never capped.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.fetch import FetchResult
from app.models import PageType
from app.site_mapper import _category_key_for_product_url, _map_site


# --- _category_key_for_product_url (pure) -----------------------------------

def test_category_segment_extracted_from_nested_product_path():
    assert _category_key_for_product_url("https://shop.example/product-category/mugs/blue-mug") == "/product-category/mugs"


def test_flat_product_urls_collapse_to_one_shared_bucket():
    assert _category_key_for_product_url("https://shop.example/product/blue-mug") == "/product"
    assert _category_key_for_product_url("https://shop.example/product/red-mug") == "/product"


# --- Wiring: real per-category caps during a mocked crawl ------------------

class _FakeRobots:
    def __init__(self, base_url: str) -> None:
        pass

    async def load(self) -> None:
        pass

    def is_allowed(self, url: str) -> bool:
        return True


def _page_type_for(url: str) -> PageType:
    if "/category/" in url:
        return PageType.COLLECTION
    if "/product/" in url:
        return PageType.PRODUCT
    return PageType.HOMEPAGE


class _FakeFetcherManyCategories:
    """Homepage links to 3 category pages, each of which links to 50 products
    - a many-category store shape. No sitemap.xml (sitemap_urls=[]) so every
    URL is discovered purely through crawl-graph links, isolating the
    collection-page-attribution path (not the sitemap-fallback path already
    covered by the pure _category_key_for_product_url tests above).
    """

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def fetch(self, url: str) -> FetchResult:
        if url == "https://shop.example/" or url == "https://shop.example":
            links = [f"https://shop.example/category/{c}" for c in ("mugs", "shirts", "hats")]
            html = "<html><body>" + "".join(f'<a href="{l}">{l}</a>' for l in links) + "</body></html>"
            return FetchResult(url=url, ok=True, status=200, html=html, text="Home", final_url=url)

        for cat in ("mugs", "shirts", "hats"):
            if url == f"https://shop.example/category/{cat}":
                links = [f"https://shop.example/product/{cat}-{i}" for i in range(50)]
                html = f"<html><body><h1>{cat.title()}</h1>" + "".join(f'<a href="{l}">{l}</a>' for l in links) + "</body></html>"
                return FetchResult(url=url, ok=True, status=200, html=html, text=cat, final_url=url)

        return FetchResult(url=url, ok=True, status=200, html="<html><body><h1>Product</h1>add to cart</body></html>", text="product", final_url=url)


@pytest.mark.asyncio
async def test_products_capped_per_category_not_by_one_flat_catalog_total(monkeypatch):
    monkeypatch.setattr("app.site_mapper._fetch_sitemap_urls", lambda base_url: _async_return([]))
    monkeypatch.setattr("app.site_mapper.RobotsChecker", _FakeRobots)
    monkeypatch.setattr("app.site_mapper.PageFetcher", _FakeFetcherManyCategories)

    def _fake_classify(url, path, is_homepage, headings, body_text_sample):
        return _page_type_for(url)

    monkeypatch.setattr("app.site_mapper.classify_page", _fake_classify)

    settings = Settings(crawl_max_pages=500, crawl_max_pages_explicit=True, crawl_max_product_pages_per_category=10, crawl_max_depth=6)

    site_map = await _map_site("https://shop.example", browser=None, settings=settings, proxy_rotator=None)

    products = [p for p in site_map.pages if p.page_type == PageType.PRODUCT]
    collections = [p for p in site_map.pages if p.page_type == PageType.COLLECTION]

    # All 3 category-listing pages crawled regardless of the product cap.
    assert len(collections) == 3
    # Each category capped at 10 products (not the first 10 discovered
    # overall, and not all 150) - real per-category representation.
    by_category = {}
    for p in products:
        cat = p.url.rsplit("-", 1)[0]
        by_category[cat] = by_category.get(cat, 0) + 1
    assert set(by_category) == {"https://shop.example/product/mugs", "https://shop.example/product/shirts", "https://shop.example/product/hats"}
    assert all(count == 10 for count in by_category.values())
    assert len(products) == 30


async def _async_return(value):
    return value


# --- Wave-truncation fairness (round-robin across a batch's source pages) --
#
# Found live against a real 111-category store (britanniagifts.us): only 26
# total products were discovered, with 10 of them from a single category and
# ~16 scattered thinly across the rest - even though no category was
# anywhere near its own per-category cap. Root cause: next_wave used to be
# built by fully appending each source page's children before moving to the
# next source page in the batch, and the following loop iteration's
# wave[:room] truncation always cuts from the *end* of that combined list -
# so whichever collection page happened to be processed first in a batch got
# its entire child list through before any other page contributed a single
# child, regardless of per-category caps (which only gate *whether* a URL is
# enqueued, not where it lands in next_wave). This test reproduces that
# shape with a tight overall page budget and confirms every category now
# gets representation in the truncated wave, not just the first one.

class _FakeFetcherManyCategoriesTightBudget:
    """Same many-category shape as _FakeFetcherManyCategories, but every
    category page is discovered in the SAME batch (all three are direct
    homepage links), so they compete for room in the same next_wave
    truncation - the exact scenario the wave-truncation-order bug needed.
    """

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def fetch(self, url: str) -> FetchResult:
        if url == "https://shop.example/" or url == "https://shop.example":
            links = [f"https://shop.example/category/{c}" for c in ("mugs", "shirts", "hats")]
            html = "<html><body>" + "".join(f'<a href="{l}">{l}</a>' for l in links) + "</body></html>"
            return FetchResult(url=url, ok=True, status=200, html=html, text="Home", final_url=url)

        for cat in ("mugs", "shirts", "hats"):
            if url == f"https://shop.example/category/{cat}":
                # Each category page lists far more products than the
                # tight remaining page budget can hold.
                links = [f"https://shop.example/product/{cat}-{i}" for i in range(50)]
                html = f"<html><body><h1>{cat.title()}</h1>" + "".join(f'<a href="{l}">{l}</a>' for l in links) + "</body></html>"
                return FetchResult(url=url, ok=True, status=200, html=html, text=cat, final_url=url)

        return FetchResult(url=url, ok=True, status=200, html="<html><body><h1>Product</h1>add to cart</body></html>", text="product", final_url=url)


@pytest.mark.asyncio
async def test_tight_overall_budget_still_represents_every_category_fairly(monkeypatch):
    monkeypatch.setattr("app.site_mapper._fetch_sitemap_urls", lambda base_url: _async_return([]))
    monkeypatch.setattr("app.site_mapper.RobotsChecker", _FakeRobots)
    monkeypatch.setattr("app.site_mapper.PageFetcher", _FakeFetcherManyCategoriesTightBudget)

    def _fake_classify(url, path, is_homepage, headings, body_text_sample):
        return _page_type_for(url)

    monkeypatch.setattr("app.site_mapper.classify_page", _fake_classify)

    # Room for: homepage(1) + 3 category pages(3) + only 10 more product
    # pages - far short of the 150 products the 3 category pages link to
    # combined, and each per-category cap (30) is nowhere close to binding.
    # Pre-fix, all 10 product slots would go to whichever category page was
    # processed first in the batch; post-fix, they should interleave.
    settings = Settings(
        crawl_max_pages=14, crawl_max_pages_explicit=True,
        crawl_max_product_pages_per_category=30, crawl_max_depth=6,
    )

    site_map = await _map_site("https://shop.example", browser=None, settings=settings, proxy_rotator=None)

    products = [p for p in site_map.pages if p.page_type == PageType.PRODUCT]
    assert len(products) == 10

    by_category = {}
    for p in products:
        cat = p.url.rsplit("-", 1)[0]
        by_category[cat] = by_category.get(cat, 0) + 1

    # Every category gets at least one product - the fairness property the
    # round-robin fix restores. Pre-fix this would be a single category
    # holding all 10 and the other two holding 0.
    assert len(by_category) == 3
    assert all(count >= 1 for count in by_category.values())
    # No category should be able to dominate to the point that another gets
    # starved out entirely: with 10 slots split round-robin across 3
    # categories, the max possible for any one category is 4 (ceil(10/3)).
    assert all(count <= 4 for count in by_category.values())
