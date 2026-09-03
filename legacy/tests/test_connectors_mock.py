from __future__ import annotations

from app.connectors.mock import MockStoreConnector
from app.models.product import ProductCategory


async def test_mock_connector_returns_full_seeded_catalog():
    connector = MockStoreConnector()
    products = await connector.fetch_products()

    assert len(products) == 12
    categories = {p.category for p in products}
    assert categories == set(ProductCategory)


async def test_mock_connector_includes_a_noncompliant_product_per_category():
    connector = MockStoreConnector()
    products = await connector.fetch_products()

    # scooter-2 is missing a GTIN and a certification mention
    scooter_2 = next(p for p in products if p.id == "scooter-2")
    assert scooter_2.gtin is None

    # playhouse-2 has no safety/age-grading language
    playhouse_2 = next(p for p in products if p.id == "playhouse-2")
    assert "warning" not in playhouse_2.description.lower()

    # tool-2 has an empty description and no images
    tool_2 = next(p for p in products if p.id == "tool-2")
    assert tool_2.description == ""
    assert tool_2.images == []
