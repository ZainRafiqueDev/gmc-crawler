"""Shared in-memory shape every extractor emits, before persistence
(app.evidence_store.persist_facts turns these into app.db.ResourceFact rows).
Kept separate from ResourceFact itself so extractor functions stay pure and
DB-free - unit-testable the same way app.page_classifier.classify_page is.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.facts.keys import confidence_for_method


@dataclass(frozen=True)
class FactRecord:
    resource_url: str
    fact: str
    value: str
    source_url: str
    source_text: str
    method: str
    confidence: float
    variant_key: str | None = None


def make_fact(
    resource_url: str, fact: str, value: str, source_url: str, source_text: str, method: str,
    variant_key: str | None = None,
) -> FactRecord:
    """Confidence is always derived from the method's fixed base confidence
    (app.facts.keys.METHOD_BASE_CONFIDENCE) - never hand-picked per instance,
    per the canonical fact dictionary's "confidence is per-method" rule.

    variant_key: pass the extracted SKU (or other stable identifier) when
    this fact belongs to one specific offer/variant on a multi-offer product
    page - see app.db.ResourceFact.variant_key's docstring for why."""
    return FactRecord(
        resource_url=resource_url, fact=fact, value=value,
        source_url=source_url, source_text=source_text, method=method,
        confidence=confidence_for_method(method), variant_key=variant_key,
    )
