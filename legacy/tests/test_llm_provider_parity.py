"""Proves the LLM abstraction actually isolates the compliance engine from
which provider is active: the same fixture, run through the Ollama adapter
and the OpenAI adapter (each mocked at the HTTP layer), must produce the
same *shape* of result."""
from __future__ import annotations

import json

import httpx
import respx

from app.llm.checks import run_llm_checks
from app.llm.ollama import OllamaProvider
from app.llm.openai_provider import OpenAIProvider
from app.models.product import Product, ProductCategory, ProductImage

PRODUCT = Product(
    id="p1", source_id="s1", title="EspressoPro Machine",
    description="WAS $499 NOW $19 - insane 96% off, hurry before it's gone!!!",
    price=19.0, landing_page_price=19.0,
    images=[ProductImage(url="https://x/img.jpg", width_px=1200, height_px=1200)],
    gtin="00012345678905", category=ProductCategory.COFFEE_MACHINE,
)

FINDINGS_JSON = json.dumps({
    "violations": [
        {"rule": "deceptive_pricing", "severity": "critical", "message": "Fake urgency / inflated discount claim."},
    ]
})


@respx.mock
async def test_ollama_and_openai_adapters_produce_same_shaped_violations():
    respx.post("http://localhost:11434/api/generate").mock(
        return_value=httpx.Response(200, json={"response": FINDINGS_JSON})
    )
    ollama_violations = await run_llm_checks(PRODUCT, OllamaProvider(host="http://localhost:11434", model="mistral"))

    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": FINDINGS_JSON}}]})
    )
    openai_violations = await run_llm_checks(PRODUCT, OpenAIProvider(api_key="sk-test"))

    assert len(ollama_violations) == len(openai_violations) == 1
    o, a = ollama_violations[0], openai_violations[0]
    assert (o.product_id, o.rule, o.severity, o.source) == (a.product_id, a.rule, a.severity, a.source)
    assert o.message == a.message
