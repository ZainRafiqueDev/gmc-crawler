"""The one normalized product shape every downstream component consumes.

Store connectors are the only code allowed to touch raw platform data;
everything past the Data Collector Agent works against this schema only.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ProductCategory(str, Enum):
    ELECTRIC_SCOOTER = "electric_scooter"
    ELECTRIC_MOTOR = "electric_motor"
    COFFEE_MACHINE = "coffee_machine"
    REFRIGERATOR = "refrigerator"
    PLAYHOUSE = "playhouse"
    HOUSEHOLD_TOOL = "household_tool"


class ProductCondition(str, Enum):
    NEW = "new"
    REFURBISHED = "refurbished"
    USED = "used"


class ProductImage(BaseModel):
    url: str
    width_px: int | None = None
    height_px: int | None = None


class Product(BaseModel):
    """Internal normalized product schema.

    `attributes` carries category-variable fields (battery_wh, motor_watts,
    age_grade_months, capacity_l, ...) as a free-form dict - the JSONB
    equivalent at the ORM layer - so adding a new category never requires
    a schema migration to this model.
    """

    id: str
    source_id: str = Field(description="ID/SKU from the originating store platform")
    title: str
    description: str = ""
    price: float | None = None
    landing_page_price: float | None = None
    currency: str = "USD"
    images: list[ProductImage] = Field(default_factory=list)
    gtin: str | None = None
    mpn: str | None = None
    category: ProductCategory
    condition: ProductCondition = ProductCondition.NEW
    availability: str = "in_stock"
    shipping_weight_kg: float | None = None
    shipping_dims_cm: dict[str, float] | None = None  # {length, width, height}
    attributes: dict[str, Any] = Field(default_factory=dict)
