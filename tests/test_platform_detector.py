import httpx
import pytest
import respx

from app.models import Platform
from app.platform_detector import detect_platform

# SSRF-guard DNS resolution is neutralized globally for fake test domains -
# see tests/conftest.py::_no_real_dns_for_ssrf_guard.


@pytest.mark.asyncio
@respx.mock
async def test_detects_woocommerce_via_rest_index():
    respx.get("https://shop.example.com/wp-json/").mock(
        return_value=httpx.Response(200, json={"namespaces": ["wp/v2", "wc/v3"]})
    )
    result = await detect_platform("shop.example.com")
    assert result.platform == Platform.WOOCOMMERCE
    assert result.base_url == "https://shop.example.com"
    assert any("wc/v3" in e for e in result.evidence)


@pytest.mark.asyncio
@respx.mock
async def test_detects_wordpress_via_rest_index():
    respx.get("https://blog.example.com/wp-json/").mock(
        return_value=httpx.Response(200, json={"namespaces": ["wp/v2", "oembed/1.0"]})
    )
    result = await detect_platform("https://blog.example.com")
    assert result.platform == Platform.WORDPRESS


@pytest.mark.asyncio
@respx.mock
async def test_falls_back_to_direct_wc_probe_when_index_disabled():
    respx.get("https://shop2.example.com/wp-json/").mock(return_value=httpx.Response(404))
    respx.get("https://shop2.example.com/wp-json/wc/v3/products").mock(
        return_value=httpx.Response(401, json={"code": "woocommerce_rest_authentication_error"})
    )
    result = await detect_platform("shop2.example.com")
    assert result.platform == Platform.WOOCOMMERCE
    assert any("wc/v3/products returned 401" in e for e in result.evidence)


@pytest.mark.asyncio
@respx.mock
async def test_falls_back_to_html_markers_when_rest_api_fully_disabled():
    respx.get("https://shop3.example.com/wp-json/").mock(return_value=httpx.Response(404))
    respx.get("https://shop3.example.com/wp-json/wc/v3/products").mock(return_value=httpx.Response(404))
    respx.get("https://shop3.example.com/wp-json/wp/v2/").mock(return_value=httpx.Response(404))
    respx.get("https://shop3.example.com/products.json").mock(return_value=httpx.Response(404))
    respx.get("https://shop3.example.com").mock(
        return_value=httpx.Response(200, text='<html><body class="woocommerce-page">hi</body></html>')
    )
    result = await detect_platform("shop3.example.com")
    assert result.platform == Platform.WOOCOMMERCE
    assert any("HTML" in e for e in result.evidence)


@pytest.mark.asyncio
@respx.mock
async def test_unknown_when_no_signals_present():
    respx.get("https://plain.example.com/wp-json/").mock(return_value=httpx.Response(404))
    respx.get("https://plain.example.com/wp-json/wc/v3/products").mock(return_value=httpx.Response(404))
    respx.get("https://plain.example.com/wp-json/wp/v2/").mock(return_value=httpx.Response(404))
    respx.get("https://plain.example.com/products.json").mock(return_value=httpx.Response(404))
    respx.get("https://plain.example.com").mock(return_value=httpx.Response(200, text="<html><body>hi</body></html>"))
    result = await detect_platform("plain.example.com")
    assert result.platform == Platform.UNKNOWN


@pytest.mark.asyncio
@respx.mock
async def test_detects_shopify_via_products_json():
    respx.get("https://shop4.example.com/wp-json/").mock(return_value=httpx.Response(404))
    respx.get("https://shop4.example.com/wp-json/wc/v3/products").mock(return_value=httpx.Response(404))
    respx.get("https://shop4.example.com/wp-json/wp/v2/").mock(return_value=httpx.Response(404))
    respx.get("https://shop4.example.com/products.json").mock(
        return_value=httpx.Response(200, json={"products": [{"id": 1, "title": "Widget", "handle": "widget"}]})
    )
    result = await detect_platform("shop4.example.com")
    assert result.platform == Platform.SHOPIFY
    assert any("products.json" in e for e in result.evidence)


@pytest.mark.asyncio
@respx.mock
async def test_detects_shopify_via_html_markers_when_products_json_blocked():
    respx.get("https://shop5.example.com/wp-json/").mock(return_value=httpx.Response(404))
    respx.get("https://shop5.example.com/wp-json/wc/v3/products").mock(return_value=httpx.Response(404))
    respx.get("https://shop5.example.com/wp-json/wp/v2/").mock(return_value=httpx.Response(404))
    respx.get("https://shop5.example.com/products.json").mock(return_value=httpx.Response(403))
    respx.get("https://shop5.example.com").mock(
        return_value=httpx.Response(200, text='<html><head><script src="https://cdn.shopify.com/s/files/theme.js"></script></head><body>hi</body></html>')
    )
    result = await detect_platform("shop5.example.com")
    assert result.platform == Platform.SHOPIFY
    assert any("HTML" in e for e in result.evidence)


@pytest.mark.asyncio
@respx.mock
async def test_adds_https_scheme_when_missing():
    respx.get("https://noscheme.example.com/wp-json/").mock(
        return_value=httpx.Response(200, json={"namespaces": ["wp/v2"]})
    )
    result = await detect_platform("noscheme.example.com")
    assert result.base_url == "https://noscheme.example.com"
