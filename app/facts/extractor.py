"""Combined entry point for every fact extractor: business identity +
shipping/returns/payment (piece 1), JSON-LD product facts (piece 2, purely
deterministic), and LLM-assisted extraction for the four "free-prose"
commerce facts that keyword-proximity regex can't reliably judge on its own
(app.facts.llm_commerce_facts). Async now because that last one can make
real LLM calls - see app.first_audit for the call site.
"""
from __future__ import annotations

from app.config import Settings
from app.facts.business_identity_facts import extract_business_identity_facts
from app.facts.commerce_facts import extract_commerce_facts
from app.facts.jsonld_facts import extract_product_jsonld_facts
from app.facts.llm_commerce_facts import extract_llm_assisted_commerce_facts
from app.facts.types import FactRecord
from app.llm.cache import LLMCache
from app.models import SiteMap


async def extract_all_facts(site_map: SiteMap, settings: Settings, cache: LLMCache | None = None) -> list[FactRecord]:
    facts = extract_business_identity_facts(site_map) + extract_commerce_facts(site_map)
    for page in site_map.pages:
        facts.extend(extract_product_jsonld_facts(page))
    facts.extend(await extract_llm_assisted_commerce_facts(site_map, settings, cache))
    return facts
