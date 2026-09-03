"""Data Collector Agent.

The single choke point between "whichever StoreConnector is active" and the
rest of the pipeline. Nothing past this module ever imports a connector
directly or knows a platform name.
"""
from __future__ import annotations

import logging

from app.connectors.base import StoreConnector
from app.models.product import Product

logger = logging.getLogger("gmc_compliance.collector")


class DataCollectorAgent:
    def __init__(self, connector: StoreConnector) -> None:
        self._connector = connector

    async def collect(self) -> list[Product]:
        products = await self._connector.fetch_products()
        logger.info("Collected %d products from %s", len(products), type(self._connector).__name__)
        return products
