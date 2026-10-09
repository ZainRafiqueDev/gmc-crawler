"""JSON-LD product fact extraction (work-order step 4, piece 2). Emits the
SAME canonical keys as piece 1's platform-API/visible-HTML product facts
(product.price.amount/.currency, product.availability, product.brand,
product.gtin, product.mpn, product.sku, product.condition), all tagged
method="jsonld" - this shared-key-different-method design is exactly what
lets the rule engine (piece 4) compare a JSON-LD price against a platform-API
price for the same product. Every normalization step reuses app.facts.normalize
(normalize_gtin, normalize_identifier, normalize_availability, normalize_condition)
- no parallel parsing path.

See app/facts/canonical_facts.md's "JSON-LD parsing decisions" section for the
documented choices behind @graph walking, AggregateOffer (lowPrice), per-variant
offers, and malformed-block handling.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

from bs4 import BeautifulSoup

from app.facts.keys import (
    PRODUCT_AVAILABILITY,
    PRODUCT_BRAND,
    PRODUCT_CONDITION,
    PRODUCT_GTIN,
    PRODUCT_MPN,
    PRODUCT_PRICE_AMOUNT,
    PRODUCT_PRICE_CURRENCY,
    PRODUCT_SKU,
)
from app.facts.normalize import normalize_availability, normalize_condition, normalize_gtin, normalize_identifier
from app.facts.types import FactRecord, make_fact
from app.models import CrawledPage, PageType

logger = logging.getLogger("gmc_audit.facts.jsonld")

_GTIN_FIELDS_BY_PRIORITY = ("gtin14", "gtin13", "gtin12", "gtin8", "gtin")
_PRICE_NUMBER_RE = re.compile(r"^\d+(\.\d+)?$")


def _parse_jsonld_blocks(html: str) -> list[object]:
    """Parses every <script type="application/ld+json"> block - a malformed
    one is logged and skipped, never raised, so one broken block can't kill
    extraction for the rest of the page."""
    soup = BeautifulSoup(html, "lxml")
    blocks: list[object] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text()
        if not raw or not raw.strip():
            continue
        try:
            blocks.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError) as exc:
            logger.debug("Skipping malformed ld+json block: %s", exc)
            continue
    return blocks


def _flatten_nodes(parsed: object) -> list[dict]:
    """Recursively flattens a parsed JSON-LD value (object, array, @graph, or
    a ProductGroup's hasVariant - in any nesting combination) into a flat
    list of plain node dicts.

    hasVariant is walked exactly like @graph - confirmed live (gymshark.com):
    modern Shopify emits a product's structured data as a top-level
    @type: "ProductGroup" wrapping its real variants under hasVariant[], each
    variant its own @type: "Product" carrying its own sku/mpn/gtin/offers.
    Before this, the flattener only ever recursed into @graph/plain arrays,
    so this entire shape silently produced zero product facts. The
    ProductGroup node itself is not kept once it has variants (it's
    @type: "ProductGroup", not "Product", so _is_product_node would reject
    it anyway) - only the real variants are. Any ProductGroup-level-only
    field (e.g. a shared "brand") that isn't repeated on each variant is a
    known, separate, narrower gap - not addressed here.
    """
    if isinstance(parsed, list):
        nodes: list[dict] = []
        for item in parsed:
            nodes.extend(_flatten_nodes(item))
        return nodes
    if isinstance(parsed, dict):
        for nested_key in ("@graph", "hasVariant"):
            nested = parsed.get(nested_key)
            if isinstance(nested, (list, dict)):
                items = nested if isinstance(nested, list) else [nested]
                nodes: list[dict] = []
                for item in items:
                    nodes.extend(_flatten_nodes(item))
                return nodes
        return [parsed]
    return []


def _node_types(node: dict) -> list[str]:
    raw_type = node.get("@type")
    if isinstance(raw_type, list):
        return [str(t) for t in raw_type]
    if raw_type:
        return [str(raw_type)]
    return []


def _is_product_node(node: dict) -> bool:
    return any(t.lower() == "product" for t in _node_types(node))


def _is_aggregate_offer(node: dict) -> bool:
    return any(t.lower() == "aggregateoffer" for t in _node_types(node))


def _normalize_offers(raw: object) -> list[dict]:
    """Offers can be a single object, a list (real variants - never
    collapsed), or an AggregateOffer (emits one lowPrice-based fact set,
    documented in canonical_facts.md, PLUS any real nested `offers` it
    carries). Returns a flat list of offer-like dicts, each tagged with
    `_is_aggregate` so the caller knows which price field to read."""
    if raw is None:
        return []
    if isinstance(raw, list):
        offers: list[dict] = []
        for item in raw:
            offers.extend(_normalize_offers(item))
        return offers
    if isinstance(raw, dict):
        if _is_aggregate_offer(raw):
            aggregate = dict(raw)
            aggregate["_is_aggregate"] = True
            return [aggregate] + _normalize_offers(raw.get("offers"))
        return [raw]
    return []


def _parse_price_number(raw: object) -> str | None:
    """Handles schema.org's real-world price representations: a plain
    numeric string ("19.99"), a JSON number (19.99 or 19), or a string with a
    thousands separator ("1,299.00"). None for anything that isn't a clean
    decimal after stripping separators - never guessed."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return str(raw)
    if isinstance(raw, str):
        cleaned = raw.replace(",", "").strip()
        if _PRICE_NUMBER_RE.match(cleaned):
            return cleaned
    return None


def _extract_brand(raw: object) -> str | None:
    if isinstance(raw, dict):
        name = raw.get("name")
        return str(name).strip() if name else None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _extract_gtin(node: dict) -> str | None:
    for field in _GTIN_FIELDS_BY_PRIORITY:
        value = node.get(field)
        if value:
            normalized = normalize_gtin(str(value))
            if normalized:
                return normalized
    return None


def _offer_price_field(offer: dict) -> object:
    return offer.get("lowPrice") if offer.get("_is_aggregate") else offer.get("price")


def _facts_for_offer(url: str, offer: dict, product_sku: object, product_gtin: str | None, product_mpn: object) -> list[FactRecord]:
    # Every one of this offer's identifiers is resolved up front (offer's
    # own field, falling back to the product/group-level one if the offer
    # doesn't carry its own - unchanged from before, still correct for the
    # FACT VALUES themselves) - then reused for both the fact values below
    # AND variant_key, rather than computed twice.
    sku_raw = offer.get("sku") or product_sku
    sku_norm = normalize_identifier(sku_raw) if sku_raw else None
    gtin = _extract_gtin(offer) or product_gtin
    mpn_raw = offer.get("mpn") or product_mpn
    mpn_norm = normalize_identifier(mpn_raw) if mpn_raw else None

    # variant_key must come from whatever actually DISTINGUISHES this offer
    # from its siblings - confirmed live (gymshark.com) a real bug deriving
    # it sku-first: a ProductGroup's variants commonly repeat the SAME sku
    # on every variant (the shared parent identifier) while gtin/mpn are
    # what genuinely differ per size/color - sku-first silently gave every
    # variant the same key, defeating "never collapse variants" the moment
    # two variants were compared (a real per-variant difference would go
    # unseen - a false negative). gtin (globally unique by design) is
    # preferred, then mpn, falling back to sku.
    #
    # Last resort (no identifier at all): a content hash of this offer's own
    # fields - NOT a per-call positional index. A ProductGroup's variants
    # are flattened into SEPARATE top-level nodes
    # (app.facts.jsonld_facts._flatten_nodes), each with its own single-
    # element offers list, so a positional index would reset to 0 for every
    # variant and silently collapse them right back together - the same bug
    # this fix exists to prevent. A content hash also happens to satisfy
    # "genuinely identical offers still group together" for free: identical
    # offer content hashes identically.
    variant_key = gtin or mpn_norm or sku_norm or hashlib.sha256(json.dumps(offer, sort_keys=True, default=str).encode()).hexdigest()[:16]
    evidence_prefix = f"variant {variant_key} (SKU {sku_norm}): " if sku_norm else f"variant {variant_key}: "

    facts: list[FactRecord] = []

    price_raw = _offer_price_field(offer)
    price = _parse_price_number(price_raw)
    if price is not None:
        price_field = "lowPrice" if offer.get("_is_aggregate") else "price"
        facts.append(make_fact(url, PRODUCT_PRICE_AMOUNT, price, url, f"{evidence_prefix}{price_field}={price_raw}", "jsonld", variant_key=variant_key))

        currency_raw = offer.get("priceCurrency")
        if currency_raw:
            facts.append(make_fact(url, PRODUCT_PRICE_CURRENCY, str(currency_raw).upper(), url, f"{evidence_prefix}priceCurrency={currency_raw}", "jsonld", variant_key=variant_key))

    availability_raw = offer.get("availability")
    if availability_raw:
        facts.append(make_fact(url, PRODUCT_AVAILABILITY, normalize_availability(availability_raw), url, f"{evidence_prefix}availability={availability_raw}", "jsonld", variant_key=variant_key))

    if sku_norm:
        facts.append(make_fact(url, PRODUCT_SKU, sku_norm, url, f"{evidence_prefix}sku={sku_raw}", "jsonld", variant_key=variant_key))

    if gtin:
        facts.append(make_fact(url, PRODUCT_GTIN, gtin, url, f"{evidence_prefix}gtin", "jsonld", variant_key=variant_key))

    if mpn_norm:
        facts.append(make_fact(url, PRODUCT_MPN, mpn_norm, url, f"{evidence_prefix}mpn={mpn_raw}", "jsonld", variant_key=variant_key))

    return facts


def _facts_from_product_node(url: str, node: dict) -> list[FactRecord]:
    facts: list[FactRecord] = []

    brand = _extract_brand(node.get("brand"))
    if brand:
        facts.append(make_fact(url, PRODUCT_BRAND, brand, url, f"brand={node.get('brand')}", "jsonld"))

    condition_raw = node.get("itemCondition")
    if condition_raw:
        facts.append(make_fact(url, PRODUCT_CONDITION, normalize_condition(condition_raw), url, f"itemCondition={condition_raw}", "jsonld"))

    product_gtin = _extract_gtin(node)
    product_sku = node.get("sku")
    product_mpn = node.get("mpn")

    offers = _normalize_offers(node.get("offers"))
    if not offers:
        # No offers at all on this node - still surface the product-level
        # identifiers if present, rather than emitting nothing.
        if product_gtin:
            facts.append(make_fact(url, PRODUCT_GTIN, product_gtin, url, "product-level gtin", "jsonld"))
        sku_norm = normalize_identifier(product_sku) if product_sku else None
        if sku_norm:
            facts.append(make_fact(url, PRODUCT_SKU, sku_norm, url, f"sku={product_sku}", "jsonld"))
        mpn_norm = normalize_identifier(product_mpn) if product_mpn else None
        if mpn_norm:
            facts.append(make_fact(url, PRODUCT_MPN, mpn_norm, url, f"mpn={product_mpn}", "jsonld"))
        return facts

    for offer in offers:
        facts.extend(_facts_for_offer(url, offer, product_sku, product_gtin, product_mpn))

    return facts


def extract_product_jsonld_facts(page: CrawledPage) -> list[FactRecord]:
    if not page.reachable or page.page_type != PageType.PRODUCT or not page.html:
        return []

    facts: list[FactRecord] = []
    for block in _parse_jsonld_blocks(page.html):
        for node in _flatten_nodes(block):
            if _is_product_node(node):
                facts.extend(_facts_from_product_node(page.url, node))
    return facts
