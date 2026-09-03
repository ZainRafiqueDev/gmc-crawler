"""Shopify connector stub - not wired up yet.

Kept here so the factory's STORE_PLATFORM=shopify branch has somewhere to
point once real implementation work starts. Follows the same
`fetch_products()` contract as every other connector.
"""
from __future__ import annotations

from app.connectors.base import StoreConnector, StoreConnectorError
from app.models.product import Product


class ShopifyConnector(StoreConnector):
    def __init__(self, store_url: str, api_key: str, api_secret: str) -> None:
        self._store_url = store_url
        self._api_key = api_key
        self._api_secret = api_secret

    async def fetch_products(self) -> list[Product]:
        raise StoreConnectorError(
            "ShopifyConnector is a stub - implement Admin API product fetch + "
            "normalization before setting STORE_PLATFORM=shopify."
        )
