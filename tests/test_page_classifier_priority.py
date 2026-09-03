from app.page_classifier import looks_like_catalog_priority_url, looks_like_overview_priority_url


def test_policy_and_contact_urls_are_overview_priority():
    for path in ["/privacy-policy", "/shipping-policy", "/returns", "/terms-of-service", "/contact-us", "/about"]:
        assert looks_like_overview_priority_url(path), path
        assert not looks_like_catalog_priority_url(path), path


def test_product_urls_are_catalog_priority_not_overview():
    for path in ["/product/red-mug", "/products/blue-mug"]:
        assert looks_like_catalog_priority_url(path), path
        assert not looks_like_overview_priority_url(path), path


def test_collection_and_blog_urls_are_neither_priority_tier():
    for path in ["/collections/mugs", "/category/mugs", "/blog/how-to-choose-a-mug"]:
        assert not looks_like_overview_priority_url(path), path
        assert not looks_like_catalog_priority_url(path), path
