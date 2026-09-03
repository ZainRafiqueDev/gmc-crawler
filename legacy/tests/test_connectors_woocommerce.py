from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx

from app.connectors.woocommerce import WooCommerceConnector, wc_product_to_internal
from app.models.product import ProductCategory

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "woocommerce_products.json"


def _load_fixture() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text())


def test_wc_product_to_internal_parses_full_product_into_normalized_shape():
    raw = _load_fixture()[0]
    product = wc_product_to_internal(raw)

    assert product.source_id == "501"
    assert product.title == "Voltway 350W Commuter Electric Scooter"
    assert product.price == 449.99
    assert product.gtin == "00012345678905"
    assert product.category == ProductCategory.ELECTRIC_SCOOTER
    assert product.shipping_weight_kg == 13.5
    assert product.shipping_dims_cm == {"length": 110.0, "width": 45.0, "height": 55.0}
    assert product.images and product.images[0].url.endswith("scooter-501.jpg")


def test_wc_product_to_internal_handles_missing_optional_fields_without_crashing():
    raw = _load_fixture()[1]
    product = wc_product_to_internal(raw)

    # Explicitly empty, not silently dropped.
    assert product.gtin is None
    assert product.images == []
    assert product.price is None
    assert product.shipping_weight_kg is None
    assert product.shipping_dims_cm is None
    assert product.category == ProductCategory.HOUSEHOLD_TOOL  # no categories -> default fallback
    assert product.title == "Bare Bones Grabber Tool"


@respx.mock
async def test_woocommerce_connector_fetch_products_hits_rest_api_and_normalizes():
    fixture = _load_fixture()
    route = respx.get("https://store.example.com/wp-json/wc/v3/products").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    connector = WooCommerceConnector(store_url="https://store.example.com", api_key="ck_x", api_secret="cs_x")
    products = await connector.fetch_products()

    assert route.called
    assert len(products) == 2
    assert {p.source_id for p in products} == {"501", "502"}
