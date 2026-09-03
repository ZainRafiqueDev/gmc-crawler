"""Store connector contract.

Every connector - mock, WooCommerce, Shopify, whatever comes next - implements
this single interface. Nothing outside this package is allowed to know which
concrete class is active; the Data Collector Agent only ever talks to
`StoreConnector.fetch_products()`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.product import Product


class StoreConnectorError(RuntimeError):
    """Raised when a connector can't reach or parse the underlying store."""


class StoreConnector(ABC):
    @abstractmethod
    async def fetch_products(self) -> list[Product]:
        """Return the full catalog, normalized to the internal Product schema."""
        raise NotImplementedError
