from app.url_canonicalize import canonical_page_key, strip_noise_query_params


def test_strips_known_woocommerce_facet_params():
    assert strip_noise_query_params("https://shop.example/product-category/mugs?on-sale=1") == "https://shop.example/product-category/mugs"
    assert strip_noise_query_params("https://shop.example/product-category/mugs?in-stock=1") == "https://shop.example/product-category/mugs"
    assert strip_noise_query_params("https://shop.example/product-category/mugs?featured=1") == "https://shop.example/product-category/mugs"


def test_strips_filter_and_query_type_prefixed_params_for_any_attribute():
    # filter_color/query_type_color are WooCommerce's built-ins, but a store
    # can define arbitrary custom attributes (filter_material, etc.) - the
    # prefix match must cover those too, not just a fixed enum.
    assert strip_noise_query_params("https://shop.example/shop?filter_color=blue&query_type_color=or") == "https://shop.example/shop"
    assert strip_noise_query_params("https://shop.example/shop?filter_material=wood") == "https://shop.example/shop"


def test_leaves_non_noise_query_params_intact():
    result = strip_noise_query_params("https://shop.example/shop?search=mug")
    assert "search=mug" in result


def test_mixed_noise_and_real_params_only_strips_noise():
    result = strip_noise_query_params("https://shop.example/shop?featured=1&search=mug")
    assert "search=mug" in result
    assert "featured" not in result


def test_canonical_page_key_strips_pagination_segment():
    assert canonical_page_key("https://shop.example/product-category/mugs/page/2") == "https://shop.example/product-category/mugs"
    assert canonical_page_key("https://shop.example/product-category/mugs/page/164/") == "https://shop.example/product-category/mugs"


def test_canonical_page_key_strips_both_facets_and_pagination():
    key = canonical_page_key("https://shop.example/product-category/mugs/page/2?on-sale=1")
    assert key == "https://shop.example/product-category/mugs"


def test_canonical_page_key_page_1_and_page_2_share_the_same_key():
    base = "https://shop.example/product-category/mugs"
    assert canonical_page_key(base) == canonical_page_key(base + "/page/2")
