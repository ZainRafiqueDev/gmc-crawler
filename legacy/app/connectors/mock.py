"""Seeded demo catalog. Active whenever STORE_PLATFORM is unset or 'mock'.

Every category has one clean product and one deliberately non-compliant
product, so the compliance engine has real violations to catch from the
first run - see the build brief's step 1.
"""
from __future__ import annotations

from app.connectors.base import StoreConnector
from app.models.product import Product, ProductCategory, ProductCondition, ProductImage

_GOOD_IMAGE = [ProductImage(url="https://cdn.example.com/img/full.jpg", width_px=1200, height_px=1200)]
_SMALL_IMAGE = [ProductImage(url="https://cdn.example.com/img/thumb.jpg", width_px=60, height_px=60)]


def _seed_catalog() -> list[Product]:
    return [
        # --- Electric scooters ---------------------------------------------
        Product(
            id="scooter-1",
            source_id="MOCK-SCT-001",
            title="Voltway 350W Commuter Electric Scooter",
            description=(
                "UL 2272 certified electric scooter with a 350W motor and 36V battery. "
                "Max speed 15mph, range 18 miles. Weighs 12.7kg (28lbs)."
            ),
            price=449.99,
            landing_page_price=449.99,
            images=_GOOD_IMAGE,
            gtin="00012345678905",
            category=ProductCategory.ELECTRIC_SCOOTER,
            condition=ProductCondition.NEW,
            shipping_weight_kg=13.5,
            shipping_dims_cm={"length": 110, "width": 45, "height": 55},
            attributes={"battery_wh": 396, "motor_watts": 350, "certification": "UL 2272"},
        ),
        Product(
            id="scooter-2",
            source_id="MOCK-SCT-002",
            title="Zoom X8 Electric Scooter",
            description="Fast electric scooter, great for commuting. Lightweight and foldable.",
            price=399.99,
            landing_page_price=399.99,
            images=_GOOD_IMAGE,
            gtin=None,  # missing GTIN -> deterministic critical
            category=ProductCategory.ELECTRIC_SCOOTER,
            condition=ProductCondition.NEW,
            shipping_weight_kg=None,
            attributes={"battery_wh": 340, "motor_watts": 300},  # no certification -> category critical
        ),
        # --- Electric motors --------------------------------------------------
        Product(
            id="motor-1",
            source_id="MOCK-MTR-001",
            title="TorqueMax 1HP Electric Motor",
            description=(
                "1HP single-phase electric motor, UL listed, CSA certified for safe household use. "
                "Includes overload protection."
            ),
            price=129.0,
            landing_page_price=129.0,
            images=_GOOD_IMAGE,
            gtin="00098765432109",
            mpn="TM-1HP-001",
            category=ProductCategory.ELECTRIC_MOTOR,
            shipping_weight_kg=8.2,
            attributes={"motor_watts": 746, "certification": "UL Listed / CSA"},
        ),
        Product(
            id="motor-2",
            source_id="MOCK-MTR-002",
            title="PowerSpin 2HP Motor",
            description="Powerful 2HP electric motor for workshop use.",
            price=189.0,
            landing_page_price=210.0,  # price mismatch -> deterministic critical
            images=_GOOD_IMAGE,
            gtin="00098765432116",
            category=ProductCategory.ELECTRIC_MOTOR,
            attributes={"motor_watts": 1492},  # no certification -> category critical
        ),
        # --- Coffee machines ----------------------------------------------
        Product(
            id="coffee-1",
            source_id="MOCK-CFM-001",
            title="BrewCraft 12-Cup Programmable Coffee Maker",
            description=(
                "12-cup drip coffee maker, 900W. New, unopened, with 2-year manufacturer warranty."
            ),
            price=59.99,
            landing_page_price=59.99,
            images=_GOOD_IMAGE,
            gtin="00011122233344",
            category=ProductCategory.COFFEE_MACHINE,
            condition=ProductCondition.NEW,
            attributes={"wattage": 900, "warranty_years": 2},
        ),
        Product(
            id="coffee-2",
            source_id="MOCK-CFM-002",
            title="EspressoPro Machine",
            description="Espresso machine, refurbished, no warranty info available.",
            price=89.99,
            landing_page_price=89.99,
            images=_SMALL_IMAGE,  # image too small -> deterministic warning/critical
            gtin="00011122233351",
            category=ProductCategory.COFFEE_MACHINE,
            condition=ProductCondition.REFURBISHED,
            attributes={"wattage": 1100},  # no warranty disclosure -> category warning
        ),
        # --- Refrigerators ---------------------------------------------------
        Product(
            id="fridge-1",
            source_id="MOCK-RFG-001",
            title="ChillPro 18cu.ft Top-Freezer Refrigerator",
            description="18 cu.ft. refrigerator, ENERGY STAR certified, 1-year parts warranty.",
            price=799.0,
            landing_page_price=799.0,
            images=_GOOD_IMAGE,
            gtin="00055566677788",
            category=ProductCategory.REFRIGERATOR,
            attributes={"capacity_l": 510, "energy_rating": "ENERGY STAR"},
        ),
        Product(
            id="fridge-2",
            source_id="MOCK-RFG-002",
            title="CoolBox Mini Fridge",
            description="Compact mini fridge for dorms and offices.",
            price=0.0,  # empty/invalid price -> deterministic critical
            landing_page_price=0.0,
            images=_GOOD_IMAGE,
            gtin="00055566677795",
            category=ProductCategory.REFRIGERATOR,
            attributes={},
        ),
        # --- Playhouses (children's product - hard-coded high scrutiny) ---
        Product(
            id="playhouse-1",
            source_id="MOCK-PLH-001",
            title="Little Oak Wooden Playhouse",
            description=(
                "Wooden outdoor playhouse. Recommended for ages 3-8 years. "
                "WARNING: Choking hazard - small parts. Adult assembly required; "
                "adult supervision recommended during use."
            ),
            price=349.0,
            landing_page_price=349.0,
            images=_GOOD_IMAGE,
            gtin="00099988877766",
            category=ProductCategory.PLAYHOUSE,
            attributes={"age_grade": "3-8 years"},
        ),
        Product(
            id="playhouse-2",
            source_id="MOCK-PLH-002",
            title="Sunny Days Playhouse",
            description="Fun playhouse for the backyard. Easy to set up, kids love it!",
            price=299.0,
            landing_page_price=299.0,
            images=_GOOD_IMAGE,
            gtin="00099988877773",
            category=ProductCategory.PLAYHOUSE,
            attributes={},  # no age grading / safety warning -> hard-coded critical
        ),
        # --- Household tools (baseline checks only) -------------------------
        Product(
            id="tool-1",
            source_id="MOCK-TL-001",
            title="DuraGrip 20pc Household Tool Set",
            description="20-piece household tool set: hammer, screwdrivers, pliers, tape measure.",
            price=34.99,
            landing_page_price=34.99,
            images=_GOOD_IMAGE,
            gtin="00044455566677",
            category=ProductCategory.HOUSEHOLD_TOOL,
            attributes={},
        ),
        Product(
            id="tool-2",
            source_id="MOCK-TL-002",
            title="FlexReach Grabber Tool",
            description="",  # empty required field -> deterministic critical
            price=14.99,
            landing_page_price=14.99,
            images=[],  # no images -> deterministic critical
            gtin=None,
            category=ProductCategory.HOUSEHOLD_TOOL,
            attributes={},
        ),
    ]


class MockStoreConnector(StoreConnector):
    """Returns a fixed, seeded catalog. No network calls, no external state."""

    def __init__(self, catalog: list[Product] | None = None) -> None:
        self._catalog = catalog if catalog is not None else _seed_catalog()

    async def fetch_products(self) -> list[Product]:
        return list(self._catalog)
