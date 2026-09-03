"""Compliance issue / report schema shared by the engine, gate, and alerts."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"


class CheckSource(str, Enum):
    DETERMINISTIC = "deterministic"
    LLM = "llm"


class Violation(BaseModel):
    product_id: str
    rule: str
    severity: Severity
    source: CheckSource
    message: str


class ProductResult(BaseModel):
    product_id: str
    violations: list[Violation] = Field(default_factory=list)
    needs_manual_review: bool = False
    review_reason: str | None = None

    @property
    def has_critical(self) -> bool:
        return any(v.severity == Severity.CRITICAL for v in self.violations)

    @property
    def has_warning(self) -> bool:
        return any(v.severity == Severity.WARNING for v in self.violations)


class ComplianceReport(BaseModel):
    scan_id: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    product_results: list[ProductResult] = Field(default_factory=list)

    @property
    def all_violations(self) -> list[Violation]:
        return [v for r in self.product_results for v in r.violations]

    @property
    def critical_count(self) -> int:
        return sum(1 for v in self.all_violations if v.severity == Severity.CRITICAL)

    @property
    def warning_count(self) -> int:
        return sum(1 for v in self.all_violations if v.severity == Severity.WARNING)

    @property
    def is_clean(self) -> bool:
        """Zero *critical* violations - the only thing the auto-connect gate checks."""
        return self.critical_count == 0

    @property
    def unresolved_violation_keys(self) -> set[tuple[str, str]]:
        """(product_id, rule) pairs - used to dedupe repeat-run alerts."""
        return {(v.product_id, v.rule) for v in self.all_violations}
