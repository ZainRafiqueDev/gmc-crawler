"""Pydantic schema for one human-authored rule in app/rules/starter_rules.yaml.

Deliberately a small, fixed condition vocabulary (see ConditionType) rather
than a free-form expression language - every condition shape the rule engine
has to interpret is enumerated here, nothing is eval()'d. Adding a genuinely
new comparison means adding a new ConditionType + a matching branch in
app.rules.engine, not writing an expression in YAML.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator

from app.models import ImpactTier, Severity


class ConditionType(str, Enum):
    NUMERIC_MISMATCH = "numeric_mismatch"
    CROSS_PAGE_CONTRADICTION = "cross_page_contradiction"
    MISSING_RESOURCE = "missing_resource"
    ALL_MISSING = "all_missing"
    EXISTING_CHECK = "existing_check"


class RuleCondition(BaseModel):
    type: ConditionType

    # numeric_mismatch: one canonical fact key, compared across exactly two
    # extraction methods for the SAME resource (app.contradiction_engine.
    # evaluate_per_resource_method_comparison) - e.g. fact=product.price.amount,
    # methods=[jsonld, platform_api]. NOT two different fact keys - piece 2's
    # JSON-LD extractor and piece-4's platform-API facts both emit the same
    # key, which is what makes this comparison possible at all.
    fact: str | None = None
    methods: list[str] = Field(default_factory=list)
    tolerance: float | None = None

    # cross_page_contradiction: the same `fact` field as above, compared
    # across PAGES (app.contradiction_engine.evaluate_cross_page_contradiction)
    # rather than across methods - `sources` names which resource_types
    # (app.models.PageType values, e.g. "returns_policy") are in scope.
    sources: list[str] = Field(default_factory=list)

    # missing_resource
    resource_type: str | None = None

    # all_missing
    fields: list[str] = Field(default_factory=list)

    # existing_check
    check: str | None = None

    @model_validator(mode="after")
    def _require_fields_for_type(self) -> "RuleCondition":
        required: dict[ConditionType, list[str]] = {
            ConditionType.NUMERIC_MISMATCH: ["fact", "methods", "tolerance"],
            ConditionType.CROSS_PAGE_CONTRADICTION: ["fact", "sources"],
            ConditionType.MISSING_RESOURCE: ["resource_type"],
            ConditionType.ALL_MISSING: ["fields"],
            ConditionType.EXISTING_CHECK: ["check"],
        }
        missing = [
            field_name for field_name in required[self.type]
            if not getattr(self, field_name)  # covers None, "", [] uniformly
        ]
        if missing:
            raise ValueError(f"condition type {self.type.value!r} requires {missing}")
        if self.type == ConditionType.NUMERIC_MISMATCH and len(self.methods) != 2:
            raise ValueError("numeric_mismatch.methods must name exactly two methods to compare")
        return self


class Rule(BaseModel):
    id: str
    scope: str
    inputs: list[str] = Field(default_factory=list)
    condition: RuleCondition
    impact: ImpactTier
    severity: Severity
    # Short, human-facing summary - the Finding title when this rule's own
    # Evaluation is converted to a finding directly (app.findings_builder).
    # Unused for an existing_check rule's evaluations specifically, since
    # those always carry the delegated check's own, more specific title.
    title: str
    description: str
    # The "exact fix" a Finding requires (spec section 2.8) - author-stated,
    # not derived, since remediation is a property of the policy area the
    # rule covers, not something inferrable from an Evaluation's evidence.
    remediation: str
    min_confidence: float = 1.0
    policy_reference: str | None = None
    # The specific official Google page that states this rule's requirement
    # (support.google.com/merchants/answer/..., or a developers.google.com/
    # google.com page) - never a generic landing page, never fabricated.
    # Explicitly None when no verified official page was found for this rule
    # (app.rules.loader logs which rules are null at load time) - a rule
    # ships without a source rather than citing an invented or approximate
    # one. Validated against an official-domain allowlist at load time
    # (app.rules.loader._validate_google_source_url).
    google_source_url: str | None = None
