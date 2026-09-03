from __future__ import annotations

from app.models.product import Product, ProductCategory, ProductImage
from app.rules.category_rules import check_battery_certification, check_playhouse_safety_language

GOOD_IMAGE = [ProductImage(url="https://x/img.jpg", width_px=1200, height_px=1200)]


def _playhouse(description: str, attributes: dict | None = None) -> Product:
    return Product(
        id="ph1", source_id="s1", title="Playhouse", description=description,
        price=299.0, landing_page_price=299.0, images=GOOD_IMAGE, gtin="00012345678905",
        category=ProductCategory.PLAYHOUSE, attributes=attributes or {},
    )


def _scooter(description: str, attributes: dict | None = None) -> Product:
    return Product(
        id="sc1", source_id="s1", title="Scooter", description=description,
        price=299.0, landing_page_price=299.0, images=GOOD_IMAGE, gtin="00012345678905",
        category=ProductCategory.ELECTRIC_SCOOTER, shipping_weight_kg=12.0, attributes=attributes or {},
    )


def test_playhouse_without_safety_language_is_flagged():
    violations = check_playhouse_safety_language(_playhouse("Fun playhouse for the backyard."))
    rules = {v.rule for v in violations}
    assert "playhouse_age_grading" in rules
    assert "playhouse_safety_warning" in rules
    assert all(v.severity == "critical" for v in violations)


def test_playhouse_with_safety_language_passes_clean():
    violations = check_playhouse_safety_language(
        _playhouse("Recommended for ages 3-8. WARNING: choking hazard, adult supervision required.")
    )
    assert violations == []


def test_scooter_without_certification_is_flagged():
    violations = check_battery_certification(_scooter("Fast electric scooter, great for commuting."))
    assert any(v.rule == "battery_safety_certification" and v.severity == "critical" for v in violations)


def test_scooter_with_certification_passes_clean():
    violations = check_battery_certification(
        _scooter("UL 2272 certified electric scooter with a 350W motor.")
    )
    assert violations == []


def test_scooter_missing_weight_is_warning_not_critical():
    product = _scooter("UL 2272 certified electric scooter.")
    product.shipping_weight_kg = None
    violations = check_battery_certification(product)
    assert len(violations) == 1
    assert violations[0].rule == "shipping_weight_present"
    assert violations[0].severity == "warning"
