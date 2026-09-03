"""LLM (fuzzy-judgment) compliance track.

Only for what deterministic code can't reliably judge: misleading claims,
deceptive pricing language, and prohibited/restricted-category signals in
free text. Hard, keyword/field-presence-checkable rules (children's product
safety language, battery certification mentions) live in
`app.rules.category_rules` instead - see that module's docstring for why.
"""
from __future__ import annotations

import json
import re

from pydantic import BaseModel, ValidationError

from app.llm.base import LLMProvider, LLMProviderError
from app.models.product import Product
from app.models.report import CheckSource, Severity, Violation

SYSTEM_PROMPT = """You are a Google Merchant Center policy compliance reviewer.
Review the given product listing text for:
1. Misleading or unsubstantiated claims (e.g. fake urgency, unverifiable superlatives).
2. Deceptive pricing language (fake discounts, artificially inflated "was" prices, bait pricing).
3. Signals that the product may belong to a prohibited or restricted GMC category
   given its stated category.

Respond with ONLY a JSON object, no prose, no markdown fences, in this exact shape:
{"violations": [{"rule": "misleading_claims"|"deceptive_pricing"|"prohibited_category", \
"severity": "critical"|"warning", "message": "short explanation"}]}
If there are no issues, respond with {"violations": []}.
"""

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class LLMFinding(BaseModel):
    rule: str
    severity: Severity
    message: str


class LLMFindingsResponse(BaseModel):
    violations: list[LLMFinding] = []


def _strip_fences(raw: str) -> str:
    return _FENCE_RE.sub("", raw.strip()).strip()


def parse_findings(raw: str) -> list[LLMFinding]:
    cleaned = _strip_fences(raw)
    try:
        data = json.loads(cleaned)
        parsed = LLMFindingsResponse.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise LLMProviderError(f"LLM returned an unparseable compliance response: {exc}") from exc
    return parsed.violations


async def classify_listing_text(llm: LLMProvider, text: str, *, context: str = "") -> list[LLMFinding]:
    user = f"{context}\n\nListing text:\n\"\"\"\n{text}\n\"\"\"" if context else f'Listing text:\n"""\n{text}\n"""'
    raw = await llm.complete(system=SYSTEM_PROMPT, user=user)
    return parse_findings(raw)


def _product_text(product: Product) -> str:
    return f"Title: {product.title}\nDescription: {product.description}\nPrice: {product.price}"


async def run_llm_checks(product: Product, llm: LLMProvider) -> list[Violation]:
    """Raises LLMProviderError on failure - the caller (compliance engine)
    is responsible for catching it and flagging the product for manual
    review rather than crashing the whole run."""
    findings = await classify_listing_text(
        llm, _product_text(product), context=f"Declared category: {product.category.value}",
    )
    return [
        Violation(
            product_id=product.id, rule=f.rule, severity=f.severity,
            source=CheckSource.LLM, message=f.message,
        )
        for f in findings
    ]
    
