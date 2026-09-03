from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from app.config import Settings
from app.db import Database, PolicyChunk
from app.llm.cache import LLMCache
from app.policy_watcher import POLICY_SOURCE_URLS, check_policy_sources


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await database.init()
    yield database
    await database.dispose()


def _mock_all_sources(html_by_id: dict[str, str]):
    for policy_id, urls in POLICY_SOURCE_URLS.items():
        # Wrapped in <h2>/<p> (not bare text) and long enough to clear the
        # chunker's minimum chunk size - matters for the Phase C indexing
        # tests below, which need chunk_html_by_headings to actually
        # produce a chunk from this mocked content.
        html = html_by_id.get(
            policy_id,
            f"<html><body><main><h2>Policy</h2><p>Real policy content for {policy_id} describing a specific requirement in enough detail.</p></main></body></html>",
        )
        for url in urls:
            respx.get(url).mock(return_value=httpx.Response(200, text=html))


@pytest.mark.asyncio
@respx.mock
async def test_first_check_records_baseline_and_reports_no_change(db):
    _mock_all_sources({})
    results = await check_policy_sources(db)
    assert len(results) == len(POLICY_SOURCE_URLS)
    assert all(r.is_first_check and not r.changed for r in results)


@pytest.mark.asyncio
@respx.mock
async def test_unchanged_content_on_second_check_reports_no_change(db):
    _mock_all_sources({})
    await check_policy_sources(db)  # baseline
    results = await check_policy_sources(db)  # same content again
    assert all(not r.changed and not r.is_first_check for r in results)


@pytest.mark.asyncio
@respx.mock
async def test_changed_content_is_detected(db):
    _mock_all_sources({"misrepresentation": "<html><body><main>Original policy text.</main></body></html>"})
    await check_policy_sources(db)  # baseline

    for url in POLICY_SOURCE_URLS["misrepresentation"]:
        respx.get(url).mock(
            return_value=httpx.Response(200, text="<html><body><main>UPDATED policy text with new requirements.</main></body></html>")
        )
    results = await check_policy_sources(db)
    changed = {r.policy_id: r.changed for r in results}
    assert changed["misrepresentation"] is True
    # other sources unchanged
    assert changed["shipping_policy"] is False


@pytest.mark.asyncio
@respx.mock
async def test_fetch_failure_for_one_source_does_not_block_others(db):
    for policy_id, urls in POLICY_SOURCE_URLS.items():
        for url in urls:
            if policy_id == "shipping_policy":
                respx.get(url).mock(return_value=httpx.Response(503))
            else:
                respx.get(url).mock(return_value=httpx.Response(200, text=f"<html><body><main>{policy_id}</main></body></html>"))

    results = await check_policy_sources(db)
    result_ids = {r.policy_id for r in results}
    assert "shipping_policy" not in result_ids
    assert len(result_ids) == len(POLICY_SOURCE_URLS) - 1


@pytest.mark.asyncio
@respx.mock
async def test_multi_url_policy_id_still_hashes_from_the_urls_that_succeed(db):
    """privacy_policy (and terms_of_service) have more than one real source
    URL - one of them being temporarily unreachable shouldn't make the
    whole policy_id un-checkable, only shipping/returns-style single-URL
    policy_ids should be skipped entirely on a fetch failure.
    """
    urls = POLICY_SOURCE_URLS["privacy_policy"]
    assert len(urls) > 1, "this test needs privacy_policy to have multiple source URLs"

    _mock_all_sources({})
    respx.get(urls[0]).mock(return_value=httpx.Response(503))

    results = await check_policy_sources(db)
    result_ids = {r.policy_id for r in results}
    assert "privacy_policy" in result_ids


def _mock_openai_embeddings():
    response = MagicMock()
    response.data = [MagicMock(embedding=[0.1, 0.2])]
    mock_client = MagicMock()
    mock_client.embeddings.create = AsyncMock(return_value=response)
    return patch("app.llm.embeddings.AsyncOpenAI", return_value=mock_client)


@pytest.mark.asyncio
@respx.mock
async def test_first_check_with_settings_builds_the_real_index(db):
    """Passing settings (with an OpenAI key) turns on Phase C: a first
    check should scrape+chunk+embed every policy_id's source pages, not
    just record a hash.
    """
    _mock_all_sources({})
    settings = Settings(openai_api_key="sk-test")
    with _mock_openai_embeddings():
        await check_policy_sources(db, settings, cache=None)

    async with db.session() as session:
        from sqlalchemy import select
        rows = (await session.execute(select(PolicyChunk).where(PolicyChunk.policy_id == "returns_refunds"))).scalars().all()
    assert len(rows) > 0


@pytest.mark.asyncio
@respx.mock
async def test_policy_change_invalidates_the_llm_cache(db):
    """A detected policy change must invalidate every cached LLM grading
    result - a finding graded against stale policy text shouldn't be
    silently trusted after the policy changes (documented in
    app/llm/cache.py's own docstring as this feature's job).
    """
    settings = Settings(openai_api_key="sk-test")
    cache = LLMCache(db)

    _mock_all_sources({"misrepresentation": "<html><body><main><h2>Policy</h2><p>Original policy text describing the requirement.</p></main></body></html>"})
    with _mock_openai_embeddings():
        await check_policy_sources(db, settings, cache)  # baseline

    await cache.set("some-unrelated-cache-key", {"result": "cached grading result"})
    assert await cache.get("some-unrelated-cache-key") is not None

    for url in POLICY_SOURCE_URLS["misrepresentation"]:
        respx.get(url).mock(
            return_value=httpx.Response(200, text="<html><body><main><h2>Policy</h2><p>UPDATED policy text with new requirements added.</p></main></body></html>")
        )
    with _mock_openai_embeddings():
        await check_policy_sources(db, settings, cache)

    assert await cache.get("some-unrelated-cache-key") is None


@pytest.mark.asyncio
@respx.mock
async def test_all_policy_ids_have_at_least_one_real_source_url():
    assert len(POLICY_SOURCE_URLS) >= 8
    for policy_id, urls in POLICY_SOURCE_URLS.items():
        assert urls, f"{policy_id} has no source URLs"
        for url in urls:
            assert url.startswith("https://support.google.com/merchants/"), (policy_id, url)
