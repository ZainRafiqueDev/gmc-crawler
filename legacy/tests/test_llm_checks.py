from __future__ import annotations

import json

import pytest

from app.llm.base import LLMProviderError
from app.llm.checks import classify_listing_text, parse_findings
from app.llm.mock import MockLLMProvider

NON_COMPLIANT_TEXT = (
    "Title: Miracle Weight-Loss Scooter Bundle\n"
    "Description: WAS $999, NOW ONLY $49 - unbelievable 95% off, ends in 1 HOUR, act now!!! "
    "Guaranteed to change your life forever, doctors hate this trick."
)
COMPLIANT_TEXT = (
    "Title: Voltway 350W Commuter Electric Scooter\n"
    "Description: UL 2272 certified electric scooter with a 350W motor and 36V battery. "
    "Max speed 15mph, range 18 miles."
)


def _responder_for(payload: dict):
    def _respond(system: str, user: str) -> str:
        return json.dumps(payload)
    return _respond


async def test_classify_listing_text_flags_known_noncompliant_sample():
    llm = MockLLMProvider(_responder_for({
        "violations": [
            {"rule": "deceptive_pricing", "severity": "critical", "message": "Fake urgency and inflated discount."},
        ]
    }))
    findings = await classify_listing_text(llm, NON_COMPLIANT_TEXT)
    assert len(findings) == 1
    assert findings[0].rule == "deceptive_pricing"
    assert findings[0].severity == "critical"


async def test_classify_listing_text_passes_known_compliant_sample():
    llm = MockLLMProvider(_responder_for({"violations": []}))
    findings = await classify_listing_text(llm, COMPLIANT_TEXT)
    assert findings == []


def test_parse_findings_strips_markdown_fences():
    raw = '```json\n{"violations": [{"rule": "misleading_claims", "severity": "warning", "message": "m"}]}\n```'
    findings = parse_findings(raw)
    assert len(findings) == 1
    assert findings[0].rule == "misleading_claims"


def test_parse_findings_raises_on_garbage_response():
    with pytest.raises(LLMProviderError):
        parse_findings("not json at all")
