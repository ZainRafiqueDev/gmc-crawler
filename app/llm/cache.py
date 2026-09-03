"""Content-hash-keyed cache for LLM-graded and vision checks (hardening
round, section 1 - "the biggest cost lever"). `CachedLLMClient` wraps a real
LLMClient and transparently implements the same interface (call_tool /
call_tool_with_image), so no check function needs to change to benefit from
it - only app/llm/factory.py needs to know the cache exists.

Cache key = sha256(provider|model|tool_name|content_signature), where
content_signature is the exact page text sent (text checks) or the image
URL (vision checks). This is also what makes re-audits of an unchanged
store cheap with no separate "skip this page" logic: if a page's text
hasn't changed, the prompt built from it is byte-identical, so the cache
key is identical, so it's a hit - automatically, as an emergent property of
hashing the real input rather than tracking page state separately.

Invalidation:
  - Content change: automatic (different content -> different key).
  - Max age: default 30 days, checked in get() - a safety net independent
    of anything else invalidating.
  - Policy index change (once Phase C's real RAG index exists): call
    invalidate_all() from that re-embed job so findings graded against
    stale policy text aren't silently trusted forever.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.db import Database, LLMCacheEntry
from app.llm.client import LLMClient

logger = logging.getLogger("gmc_audit.llm.cache")

DEFAULT_MAX_AGE_DAYS = 30


def compute_cache_key(provider: str, model: str, tool_name: str, content_signature: str) -> str:
    raw = f"{provider}|{model}|{tool_name}|{content_signature}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class LLMCache:
    def __init__(self, db: Database, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> None:
        self.db = db
        self.max_age_days = max_age_days
        self.hits = 0
        self.misses = 0

    async def get(self, cache_key: str) -> dict | None:
        async with self.db.session() as session:
            row = (await session.execute(
                select(LLMCacheEntry).where(LLMCacheEntry.cache_key == cache_key)
            )).scalar_one_or_none()

        if row is None:
            self.misses += 1
            return None

        created_at = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created_at > timedelta(days=self.max_age_days):
            self.misses += 1
            return None

        self.hits += 1
        return json.loads(row.result_json)

    async def set(self, cache_key: str, result: dict) -> None:
        async with self.db.session() as session:
            existing = (await session.execute(
                select(LLMCacheEntry).where(LLMCacheEntry.cache_key == cache_key)
            )).scalar_one_or_none()
            if existing is not None:
                existing.result_json = json.dumps(result)
                existing.created_at = datetime.now(timezone.utc)
            else:
                session.add(LLMCacheEntry(cache_key=cache_key, result_json=json.dumps(result)))
            await session.commit()

    async def invalidate_all(self) -> int:
        """Call this once Phase C's policy re-embed job detects a policy
        change - every cached grading result was produced against the old
        policy text and shouldn't be silently trusted after that.
        """
        async with self.db.session() as session:
            count = (await session.execute(select(LLMCacheEntry))).scalars().all()
            n = len(count)
            await session.execute(delete(LLMCacheEntry))
            await session.commit()
            logger.info("Invalidated %d cached LLM result(s)", n)
            return n


class CachedLLMClient:
    """Drop-in LLMClient wrapper. Marks a cache hit by injecting
    `_from_cache: True` into the returned dict - check functions read this
    via result.get("_from_cache", False) when building the Finding, rather
    than needing their own cache-awareness.
    """

    def __init__(self, inner: LLMClient, cache: LLMCache, provider: str, model: str) -> None:
        self._inner = inner
        self._cache = cache
        self._provider = provider
        self._model = model

    async def call_tool(self, system: str, user: str, tool_name: str, tool_schema: dict, max_tokens: int = 1024) -> dict | None:
        key = compute_cache_key(self._provider, self._model, tool_name, f"{system}\n---\n{user}")
        cached = await self._cache.get(key)
        if cached is not None:
            return {**cached, "_from_cache": True}

        result = await self._inner.call_tool(system, user, tool_name, tool_schema, max_tokens)
        if result is not None:
            await self._cache.set(key, result)
        return result

    async def call_tool_with_image(
        self, system: str, user_text: str, image_url: str, tool_name: str, tool_schema: dict, max_tokens: int = 1024,
    ) -> dict | None:
        # Per spec: vision cache key is model + image URL, not the full page
        # text - the image itself is the expensive/cacheable unit here.
        key = compute_cache_key(self._provider, self._model, tool_name, image_url)
        cached = await self._cache.get(key)
        if cached is not None:
            return {**cached, "_from_cache": True}

        result = await self._inner.call_tool_with_image(system, user_text, image_url, tool_name, tool_schema, max_tokens)
        if result is not None:
            await self._cache.set(key, result)
        return result
