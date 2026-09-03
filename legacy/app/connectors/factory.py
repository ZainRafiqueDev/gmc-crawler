"""Selects the active StoreConnector from config. The only place that does."""
from __future__ import annotations

from app.config import Settings, StorePlatform
from app.connectors.base import StoreConnector
from app.connectors.mock import MockStoreConnector
from app.connectors.shopify import ShopifyConnector
from app.connectors.woocommerce import WooCommerceConnector


def get_store_connector(settings: Settings) -> StoreConnector:
    if settings.store_platform == StorePlatform.MOCK:
        return MockStoreConnector()

    if settings.store_platform == StorePlatform.WOOCOMMERCE:
        return WooCommerceConnector(
            store_url=settings.store_url or "",
            api_key=settings.store_api_key or "",
            api_secret=settings.store_api_secret or "",
        )

    if settings.store_platform == StorePlatform.SHOPIFY:
        return ShopifyConnector(
            store_url=settings.store_url or "",
            api_key=settings.store_api_key or "",
            api_secret=settings.store_api_secret or "",
        )

    raise ValueError(f"Unknown store platform: {settings.store_platform}")
