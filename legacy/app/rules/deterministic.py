"""Deterministic (zero-LLM, zero-cost) compliance checks.

Every function here takes a `Product` and returns the list of `Violation`s
it finds - an empty list means that rule passed clean. `run_deterministic_checks`
composes the baseline rules (every category) with whatever
`category_rules.CATEGORY_CHECKS` adds for that product's category.
"""
from __future__ import annotations

from app.models.product import Product
from app.models.report import CheckSource, Severity, Violation

MIN_IMAGE_PX = 100
RECOMMENDED_IMAGE_PX = 800
PRICE_TOLERANCE = 0.01


def _violation(product: Product, rule: str, severity: Severity, message: str) -> Violation:
    return Violation(
        product_id=product.id,
        rule=rule,
        severity=severity,
        source=CheckSource.DETERMINISTIC,
        message=message,
    )


def check_gtin_present(product: Product) -> list[Violation]:
    if not product.gtin or not product.gtin.strip():
        return [_violation(
            product, "gtin_present", Severity.CRITICAL,
            "Product has no GTIN. GMC requires a valid GTIN/MPN for most categories.",
        )]
    return []


def check_price_match(product: Product) -> list[Violation]:
    if product.price is None or product.landing_page_price is None:
        return [_violation(
            product, "price_match", Severity.CRITICAL,
            "Price or landing page price is missing - cannot verify price match.",
        )]
    if abs(product.price - product.landing_page_price) > PRICE_TOLERANCE:
        return [_violation(
            product, "price_match", Severity.CRITICAL,
            f"Feed price ${product.price:.2f} does not match landing page price "
            f"${product.landing_page_price:.2f}.",
        )]
    return []


def check_price_positive(product: Product) -> list[Violation]:
    if product.price is None or product.price <= 0:
        return [_violation(
            product, "price_positive", Severity.CRITICAL,
            "Product price is missing or not greater than zero.",
        )]
    return []


def check_image_quality(product: Product) -> list[Violation]:
    if not product.images:
        return [_violation(
            product, "image_quality", Severity.CRITICAL,
            "Product has no images. GMC requires at least one product image.",
        )]
    violations: list[Violation] = []
    for img in product.images:
        if img.width_px is None or img.height_px is None:
            continue  # dimensions unknown (e.g. platform didn't report them) - can't judge, skip
        if img.width_px < MIN_IMAGE_PX or img.height_px < MIN_IMAGE_PX:
            violations.append(_violation(
                product, "image_quality", Severity.CRITICAL,
                f"Image {img.url} is {img.width_px}x{img.height_px}px, below the "
                f"{MIN_IMAGE_PX}x{MIN_IMAGE_PX}px minimum.",
            ))
        elif img.width_px < RECOMMENDED_IMAGE_PX or img.height_px < RECOMMENDED_IMAGE_PX:
            violations.append(_violation(
                product, "image_quality", Severity.WARNING,
                f"Image {img.url} is {img.width_px}x{img.height_px}px, below the "
                f"recommended {RECOMMENDED_IMAGE_PX}x{RECOMMENDED_IMAGE_PX}px.",
            ))
    return violations


def check_required_fields(product: Product) -> list[Violation]:
    violations: list[Violation] = []
    if not product.title or not product.title.strip():
        violations.append(_violation(
            product, "required_fields", Severity.CRITICAL, "Product title is empty.",
        ))
    if not product.description or not product.description.strip():
        violations.append(_violation(
            product, "required_fields", Severity.CRITICAL, "Product description is empty.",
        ))
    return violations


BASELINE_CHECKS = [
    check_gtin_present,
    check_price_match,
    check_price_positive,
    check_image_quality,
    check_required_fields,
]


def run_deterministic_checks(product: Product, category_checks: list | None = None) -> list[Violation]:
    """Run baseline checks plus explicitly-passed category checks.

    Most callers want `run_all_deterministic_checks`, which resolves category
    checks automatically from `category_rules`. This lower-level function
    exists mainly so tests can exercise the baseline in isolation.
    """
    violations: list[Violation] = []
    for check in BASELINE_CHECKS:
        violations.extend(check(product))
    for check in category_checks or []:
        violations.extend(check(product))
    return violations


def run_all_deterministic_checks(product: Product) -> list[Violation]:
    from app.rules.category_rules import get_category_checks

    return run_deterministic_checks(product, get_category_checks(product.category))
