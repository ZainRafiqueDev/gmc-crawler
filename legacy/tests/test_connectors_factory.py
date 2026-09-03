from __future__ import annotations

from app.connectors.factory import get_store_connector
from app.connectors.mock import MockStoreConnector
from app.connectors.shopify import ShopifyConnector
from app.connectors.woocommerce import WooCommerceConnector


def test_factory_selects_mock_connector_by_default(make_settings):
    settings = make_settings()
    connector = get_store_connector(settings)
    assert isinstance(connector, MockStoreConnector)


def test_factory_selects_woocommerce_connector(make_settings):
    settings = make_settings(store_platform="woocommerce", store_url="https://store.example.com",
                              store_api_key="ck", store_api_secret="cs")
    connector = get_store_connector(settings)
    assert isinstance(connector, WooCommerceConnector)


def test_factory_selects_shopify_connector(make_settings):
    settings = make_settings(store_platform="shopify", store_url="https://store.myshopify.com",
                              store_api_key="k", store_api_secret="s")
    connector = get_store_connector(settings)
    assert isinstance(connector, ShopifyConnector)
