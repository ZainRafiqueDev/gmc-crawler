"""WooCommerce REST API (v3) connector.

Talks to `{store_url}/wp-json/wc/v3/products` over plain httpx so the whole
call surface can be faked with respx/httpx mock transports in tests - no
live store needed, and no requests-vs-httpx mocking mismatch with the rest
of the test suite.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.connectors.base import StoreConnector, StoreConnectorError
from app.models.product import Product, ProductCategory, ProductCondition, ProductImage

logger = logging.getLogger("gmc_compliance.connectors.woocommerce")

_CATEGORY_KEYWORDS: dict[str, ProductCategory] = {
    "scooter": ProductCategory.ELECTRIC_SCOOTER,
    "electric scooter": ProductCategory.ELECTRIC_SCOOTER,
    "motor": ProductCategory.ELECTRIC_MOTOR,
    "electric motor": ProductCategory.ELECTRIC_MOTOR,
    "coffee": ProductCategory.COFFEE_MACHINE,
    "espresso": ProductCategory.COFFEE_MACHINE,
    "refrigerator": ProductCategory.REFRIGERATOR,
    "fridge": ProductCategory.REFRIGERATOR,
    "playhouse": ProductCategory.PLAYHOUSE,
    "play house": ProductCategory.PLAYHOUSE,
}


def _map_category(wc_categories: list[dict[str, Any]]) -> ProductCategory:
    for cat in wc_categories or []:
        name = str(cat.get("name") or cat.get("slug") or "").lower()
        for keyword, mapped in _CATEGORY_KEYWORDS.items():
            if keyword in name:
                return mapped
    return ProductCategory.HOUSEHOLD_TOOL


def _find_meta(meta_data: list[dict[str, Any]], *keys: str) -> str | None:
    lowered = {k.lower() for k in keys}
    for entry in meta_data or []:
        key = str(entry.get("key", "")).lstrip("_").lower()
        if key in lowered:
            value = entry.get("value")
            return str(value) if value not in (None, "") else None
    return None


def _parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def wc_product_to_internal(raw: dict[str, Any]) -> Product:
    """Normalize one WooCommerce REST API product object into `Product`.

    Optional fields that are absent in `raw` come through as explicit
    None/empty values (never silently dropped) so downstream compliance
    checks see the same shape regardless of connector.
    """
    meta_data = raw.get("meta_data") or []
    dims = raw.get("dimensions") or {}
    shipping_dims = None
    if any(dims.get(k) not in (None, "") for k in ("length", "width", "height")):
        shipping_dims = {
            "length": _parse_float(dims.get("length")) or 0.0,
            "width": _parse_float(dims.get("width")) or 0.0,
            "height": _parse_float(dims.get("height")) or 0.0,
        }

    price = _parse_float(raw.get("price") if raw.get("price") not in (None, "") else raw.get("regular_price"))
    condition_raw = _find_meta(meta_data, "condition")
    try:
        condition = ProductCondition(condition_raw.lower()) if condition_raw else ProductCondition.NEW
    except ValueError:
        condition = ProductCondition.NEW

    return Product(
        id=f"woocommerce-{raw.get('id')}",
        source_id=str(raw.get("id")),
        title=raw.get("name") or "",
        description=raw.get("description") or raw.get("short_description") or "",
        price=price,
        landing_page_price=price,
        images=[
            ProductImage(url=img.get("src", ""), width_px=None, height_px=None)
            for img in (raw.get("images") or [])
            if img.get("src")
        ],
        gtin=_find_meta(meta_data, "gtin", "ean", "upc"),
        mpn=raw.get("sku") or None,
        category=_map_category(raw.get("categories") or []),
        condition=condition,
        availability="in_stock" if raw.get("stock_status") == "instock" else raw.get("stock_status", "out_of_stock"),
        shipping_weight_kg=_parse_float(raw.get("weight")),
        shipping_dims_cm=shipping_dims,
        attributes={a.get("key"): a.get("value") for a in (raw.get("attributes") or []) if a.get("key")},
    )


class WooCommerceConnector(StoreConnector):
    def __init__(self, store_url: str, api_key: str, api_secret: str, page_size: int = 100) -> None:
        self._base_url = store_url.rstrip("/") + "/wp-json/wc/v3"
        self._auth = (api_key, api_secret)
        self._page_size = page_size

    async def fetch_products(self) -> list[Product]:
        products: list[Product] = []
        async with httpx.AsyncClient(auth=self._auth, timeout=30.0) as client:
            page = 1
            while True:
                try:
                    resp = await client.get(
                        f"{self._base_url}/products",
                        params={"per_page": self._page_size, "page": page},
                    )
                    resp.raise_for_status()
                except httpx.HTTPError as exc:
                    raise StoreConnectorError(f"WooCommerce API request failed: {exc}") from exc

                batch = resp.json()
                if not batch:
                    break
                for raw in batch:
                    try:
                        products.append(wc_product_to_internal(raw))
                    except Exception as exc:  # noqa: BLE001 - isolate one bad record, don't kill the pull
                        logger.warning("Skipping unparseable WooCommerce product %s: %s", raw.get("id"), exc)
                if len(batch) < self._page_size:
                    break
                page += 1
        return products
