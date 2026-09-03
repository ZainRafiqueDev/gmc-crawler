"""category -> extra required checks.

This is the module the build brief calls out explicitly: rather than one
flat ruleset for every product, each `ProductCategory` maps to additional
deterministic checks layered on top of `deterministic.BASELINE_CHECKS`.

Keep hard, keyword/field-presence-checkable rules here (deterministic,
free, consistent) rather than leaving them to the LLM track, per the
build brief: "This should be a hard-coded check, not left to the LLM to
catch inconsistently."
"""
from __future__ import annotations

from app.models.product import Product, ProductCategory, ProductCondition
from app.models.report import CheckSource, Severity, Violation

_SAFETY_WARNING_KEYWORDS = ("warning", "choking hazard", "supervision", "adult assembly", "not suitable for")
_AGE_GRADE_KEYWORDS = ("age", "years", "months", "yrs")
_CERTIFICATION_KEYWORDS = ("ul ", "ul2272", "ul 2272", "csa", "certified", "certification", "iec 62133")
_WARRANTY_KEYWORDS = ("warranty", "guarantee")


def _violation(product: Product, rule: str, severity: Severity, message: str) -> Violation:
    return Violation(
        product_id=product.id, rule=rule, severity=severity,
        source=CheckSource.DETERMINISTIC, message=message,
    )


def _text_blob(product: Product) -> str:
    attr_text = " ".join(str(v) for v in product.attributes.values())
    return f"{product.title} {product.description} {attr_text}".lower()


# --- Playhouses: children's products, high scrutiny -------------------------

def check_playhouse_safety_language(product: Product) -> list[Violation]:
    blob = _text_blob(product)
    has_age_grade = bool(product.attributes.get("age_grade")) or any(k in blob for k in _AGE_GRADE_KEYWORDS)
    has_safety_warning = any(k in blob for k in _SAFETY_WARNING_KEYWORDS)

    violations: list[Violation] = []
    if not has_age_grade:
        violations.append(_violation(
            product, "playhouse_age_grading", Severity.CRITICAL,
            "Children's product (playhouse) is missing age-grading information.",
        ))
    if not has_safety_warning:
        violations.append(_violation(
            product, "playhouse_safety_warning", Severity.CRITICAL,
            "Children's product (playhouse) is missing required safety-warning language.",
        ))
    return violations


# --- Electric scooters / motors: battery & electrical scrutiny --------------

def check_battery_certification(product: Product) -> list[Violation]:
    blob = _text_blob(product)
    has_cert = bool(product.attributes.get("certification")) or any(k in blob for k in _CERTIFICATION_KEYWORDS)
    violations: list[Violation] = []
    if not has_cert:
        violations.append(_violation(
            product, "battery_safety_certification", Severity.CRITICAL,
            "Battery/electrical product is missing a safety certification mention "
            "(e.g. UL 2272, CSA) - high risk of GMC restricted-category disapproval.",
        ))
    if product.shipping_weight_kg is None:
        violations.append(_violation(
            product, "shipping_weight_present", Severity.WARNING,
            "Battery/electrical product has no shipping weight - required for accurate "
            "shipping claims on heavy items.",
        ))
    return violations


# --- Coffee machines / refrigerators: appliance rules ------------------------

def check_appliance_disclosures(product: Product) -> list[Violation]:
    blob = _text_blob(product)
    violations: list[Violation] = []
    if product.condition != ProductCondition.NEW and not any(k in blob for k in _WARRANTY_KEYWORDS):
        violations.append(_violation(
            product, "warranty_disclosure", Severity.WARNING,
            f"Condition is '{product.condition.value}' but no warranty information is disclosed.",
        ))
    makes_energy_claim = "energy star" in blob or "energy-star" in blob
    if makes_energy_claim and not product.attributes.get("energy_rating"):
        violations.append(_violation(
            product, "energy_claim_substantiation", Severity.WARNING,
            "Description makes an ENERGY STAR claim not backed by an energy_rating attribute.",
        ))
    return violations


CATEGORY_CHECKS: dict[ProductCategory, list] = {
    ProductCategory.PLAYHOUSE: [check_playhouse_safety_language],
    ProductCategory.ELECTRIC_SCOOTER: [check_battery_certification],
    ProductCategory.ELECTRIC_MOTOR: [check_battery_certification],
    ProductCategory.COFFEE_MACHINE: [check_appliance_disclosures],
    ProductCategory.REFRIGERATOR: [check_appliance_disclosures],
    ProductCategory.HOUSEHOLD_TOOL: [],  # baseline checks only
}


def get_category_checks(category: ProductCategory) -> list:
    return CATEGORY_CHECKS.get(category, [])
