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
