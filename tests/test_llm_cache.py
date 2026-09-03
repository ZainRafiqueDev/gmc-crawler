from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db import Database, LLMCacheEntry
from app.llm.cache import CachedLLMClient, LLMCache, compute_cache_key


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/cache_test.db")
    await database.init()
    yield database
    await database.dispose()


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def call_tool(self, system, user, tool_name, tool_schema, max_tokens=1024):
        self.calls += 1
        return self._responses.pop(0)

    async def call_tool_with_image(self, system, user_text, image_url, tool_name, tool_schema, max_tokens=1024):
        self.calls += 1
        return self._responses.pop(0)


def test_compute_cache_key_is_stable_and_content_sensitive():
    a = compute_cache_key("claude", "model-x", "tool", "some content")
    b = compute_cache_key("claude", "model-x", "tool", "some content")
    c = compute_cache_key("claude", "model-x", "tool", "different content")
    assert a == b
    assert a != c


@pytest.mark.asyncio
async def test_cache_miss_then_hit(db):
    cache = LLMCache(db)
    key = compute_cache_key("claude", "m", "t", "content")
    assert await cache.get(key) is None
    assert cache.misses == 1

    await cache.set(key, {"ok": True})
    result = await cache.get(key)
    assert result == {"ok": True}
    assert cache.hits == 1


@pytest.mark.asyncio
async def test_cache_expires_after_max_age(db):
    cache = LLMCache(db, max_age_days=30)
    key = compute_cache_key("claude", "m", "t", "content")
    await cache.set(key, {"ok": True})

    # simulate an old entry by directly backdating created_at
    async with db.session() as session:
        row = (await session.execute(select(LLMCacheEntry).where(LLMCacheEntry.cache_key == key))).scalar_one()
        row.created_at = datetime.now(timezone.utc) - timedelta(days=31)
        await session.commit()

    assert await cache.get(key) is None


@pytest.mark.asyncio
async def test_invalidate_all_clears_everything(db):
    cache = LLMCache(db)
    await cache.set(compute_cache_key("c", "m", "t1", "a"), {"x": 1})
    await cache.set(compute_cache_key("c", "m", "t2", "b"), {"x": 2})

    count = await cache.invalidate_all()
    assert count == 2
    assert await cache.get(compute_cache_key("c", "m", "t1", "a")) is None


@pytest.mark.asyncio
async def test_cached_client_only_calls_inner_once_for_same_content(db):
    cache = LLMCache(db)
    fake = FakeClient([{"meets_requirement": True}])
    client = CachedLLMClient(fake, cache, provider="claude", model="m")

    r1 = await client.call_tool("system", "same page text", "submit", {})
    r2 = await client.call_tool("system", "same page text", "submit", {})

    assert fake.calls == 1  # second call was a cache hit, no new API call
    assert r1 == {"meets_requirement": True}
    assert r2 == {"meets_requirement": True, "_from_cache": True}


@pytest.mark.asyncio
async def test_cached_client_calls_inner_again_for_different_content(db):
    cache = LLMCache(db)
    fake = FakeClient([{"result": "a"}, {"result": "b"}])
    client = CachedLLMClient(fake, cache, provider="claude", model="m")

    r1 = await client.call_tool("system", "page text v1", "submit", {})
    r2 = await client.call_tool("system", "page text v2 (changed)", "submit", {})

    assert fake.calls == 2  # different content -> different cache key -> both are real calls
    assert r1 == {"result": "a"}
    assert r2 == {"result": "b"}


@pytest.mark.asyncio
async def test_cached_client_vision_keys_on_image_url_not_full_text(db):
    cache = LLMCache(db)
    fake = FakeClient([{"plausible_match": True}])
    client = CachedLLMClient(fake, cache, provider="claude", model="m")

    r1 = await client.call_tool_with_image("system", "product text A", "https://shop.example/img.jpg", "submit", {})
    r2 = await client.call_tool_with_image("system", "product text B (different)", "https://shop.example/img.jpg", "submit", {})

    assert fake.calls == 1  # same image URL -> cache hit despite different surrounding text
    assert r2 == {"plausible_match": True, "_from_cache": True}


@pytest.mark.asyncio
async def test_cached_client_does_not_cache_none_results(db):
    cache = LLMCache(db)
    fake = FakeClient([None, {"ok": True}])
    client = CachedLLMClient(fake, cache, provider="claude", model="m")

    r1 = await client.call_tool("system", "text", "submit", {})
    r2 = await client.call_tool("system", "text", "submit", {})

    assert r1 is None
    assert r2 == {"ok": True}
    assert fake.calls == 2  # a failed call must not be cached and silently reused
