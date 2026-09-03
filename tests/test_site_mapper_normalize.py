"""app.site_mapper._normalize is the crawl's seen-URL dedup key - verifying
facet-query variants collapse onto the same key here (not just in
app.url_canonicalize's own unit tests) proves the crawler will actually
skip re-fetching them, not just that the stripping function works in
isolation."""
from app.site_mapper import _normalize


def test_facet_query_variants_normalize_to_the_same_key_as_the_base_url():
    base = _normalize("https://shop.example/product-category/mugs")
    assert _normalize("https://shop.example/product-category/mugs?on-sale=1") == base
    assert _normalize("https://shop.example/product-category/mugs?filter_color=blue&query_type_color=or") == base
    assert _normalize("https://shop.example/product-category/mugs?in-stock=1") == base


def test_pagination_urls_do_not_collapse_to_the_base_url():
    # Pagination is deliberately NOT collapsed at crawl time (see
    # app/url_canonicalize.py's module docstring) - page 2 must still be
    # fetched to discover products not linked from page 1.
    base = _normalize("https://shop.example/product-category/mugs")
    page_2 = _normalize("https://shop.example/product-category/mugs/page/2")
    assert page_2 != base
