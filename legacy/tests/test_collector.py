from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx

from app.collector import DataCollectorAgent
from app.connectors.mock import MockStoreConnector
from app.connectors.woocommerce import WooCommerceConnector

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "woocommerce_products.json"


async def test_collector_pulls_full_catalog_from_mock_connector():
    agent = DataCollectorAgent(MockStoreConnector())
    products = await agent.collect()
    assert len(products) == 12
    assert {p.id for p in products} == {
        "scooter-1", "scooter-2", "motor-1", "motor-2", "coffee-1", "coffee-2",
        "fridge-1", "fridge-2", "playhouse-1", "playhouse-2", "tool-1", "tool-2",
    }


@respx.mock
async def test_collector_normalization_identical_shape_across_connectors():
    respx.get("https://store.example.com/wp-json/wc/v3/products").mock(
        return_value=httpx.Response(200, json=json.loads(FIXTURE_PATH.read_text()))
    )
    woo_agent = DataCollectorAgent(
        WooCommerceConnector(store_url="https://store.example.com", api_key="k", api_secret="s")
    )
    mock_agent = DataCollectorAgent(MockStoreConnector())

    woo_products = await woo_agent.collect()
    mock_products = await mock_agent.collect()

    # Same normalized field set on both, regardless of which connector fed it.
    woo_fields = set(woo_products[0].model_dump().keys())
    mock_fields = set(mock_products[0].model_dump().keys())
    assert woo_fields == mock_fields
