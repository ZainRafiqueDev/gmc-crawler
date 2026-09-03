"""OpenAI text-embedding-3-small wrapper for the policy RAG index (Phase C).
Used regardless of Settings.llm_provider - Claude has no embeddings API, so
this always calls OpenAI directly rather than going through the provider-
agnostic LLMClient Protocol (which only covers grading/vision tool-calls).

Reuses the existing LLMCache (app/llm/cache.py) so re-embedding an unchanged
chunk or an unchanged page's query text on a later run is a cache hit, not a
fresh API call - the same content-hash-keyed pattern already used for LLM
grading/vision, and the same reason repeat audits of an unchanged store stay
cheap.
"""
from __future__ import annotations

import logging

from openai import AsyncOpenAI

from app.config import Settings
from app.llm.cache import LLMCache, compute_cache_key

logger = logging.getLogger("gmc_audit.llm.embeddings")


async def embed_text(text: str, settings: Settings, cache: LLMCache | None = None) -> list[float]:
    key = compute_cache_key("openai", settings.openai_embedding_model, "embed", text)
    if cache is not None:
        cached = await cache.get(key)
        if cached is not None:
            return cached["embedding"]

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    response = await client.embeddings.create(model=settings.openai_embedding_model, input=text)
    embedding = response.data[0].embedding

    if cache is not None:
        await cache.set(key, {"embedding": embedding})
    return embedding
