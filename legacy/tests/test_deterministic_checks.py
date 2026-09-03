"""Deterministic track only — no LLM calls anywhere in this file."""
from __future__ import annotations

from app.models.product import Product, ProductCategory, ProductImage
from app.rules.deterministic import (
    check_gtin_present,
    check_image_quality,
    check_price_match,
    check_price_positive,
    check_required_fields,
)

GOOD_IMAGE = [ProductImage(url="https://x/img.jpg", width_px=1200, height_px=1200)]


def _base_product(**overrides) -> Product:
    defaults = dict(
        id="p1", source_id="s1", title="A Tool", description="A fine tool.",
        price=10.0, landing_page_price=10.0, images=GOOD_IMAGE, gtin="00012345678905",
        category=ProductCategory.HOUSEHOLD_TOOL,
    )
    defaults.update(overrides)
    return Product(**defaults)


def test_gtin_present_pass():
    assert check_gtin_present(_base_product(gtin="00012345678905")) == []


def test_gtin_present_fail():
    violations = check_gtin_present(_base_product(gtin=None))
    assert len(violations) == 1
    assert violations[0].rule == "gtin_present"
    assert violations[0].severity == "critical"


def test_price_match_pass():
    assert check_price_match(_base_product(price=10.0, landing_page_price=10.0)) == []


def test_price_match_fail():
    violations = check_price_match(_base_product(price=10.0, landing_page_price=12.0))
    assert len(violations) == 1
    assert violations[0].rule == "price_match"


def test_price_positive_pass():
    assert check_price_positive(_base_product(price=5.0)) == []


def test_price_positive_fail():
    violations = check_price_positive(_base_product(price=0.0))
    assert len(violations) == 1
    assert violations[0].rule == "price_positive"


def test_image_quality_pass():
    assert check_image_quality(_base_product(images=GOOD_IMAGE)) == []


def test_image_quality_fail_no_images():
    violations = check_image_quality(_base_product(images=[]))
    assert len(violations) == 1
    assert violations[0].severity == "critical"


def test_image_quality_fail_below_minimum():
    small = [ProductImage(url="https://x/thumb.jpg", width_px=50, height_px=50)]
    violations = check_image_quality(_base_product(images=small))
    assert len(violations) == 1
    assert violations[0].severity == "critical"


def test_required_fields_pass():
    assert check_required_fields(_base_product(title="X", description="Y")) == []


def test_required_fields_fail_empty_description():
    violations = check_required_fields(_base_product(description=""))
    assert len(violations) == 1
    assert violations[0].rule == "required_fields"
