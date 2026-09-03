"""Unit tests for the real RAG retrieval/indexing layer (Phase C). OpenAI
embeddings are mocked (deterministic vectors, not real semantic similarity)
so these prove the retrieval/storage/fallback logic is correct - real
semantic quality is what the live validation run against a real store is
for.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from app.config import Settings
from app.db import Database, PolicyChunk
from app.llm.cache import LLMCache
from app.llm.policy_rag import get_policy_context, rebuild_policy_index

SETTINGS = Settings(openai_api_key="sk-test")


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await database.init()
    yield database
    await database.dispose()


def _mock_openai_embeddings(vectors_by_text: dict[str, list[float]] | None = None, default_vector: list[float] | None = None):
    async def fake_create(model, input):
        vec = None
        if vectors_by_text is not None:
            vec = vectors_by_text.get(input)
        if vec is None:
            vec = default_vector if default_vector is not None else [0.1, 0.1, 0.1]
        response = MagicMock()
        response.data = [MagicMock(embedding=vec)]
        return response

    mock_client = MagicMock()
    mock_client.embeddings.create = AsyncMock(side_effect=fake_create)
    return patch("app.llm.embeddings.AsyncOpenAI", return_value=mock_client)


@pytest.mark.asyncio
async def test_falls_back_to_stub_snippet_when_no_db():
    ctx = await get_policy_context("returns_refunds", "some page text", SETTINGS, db=None, cache=None)
    assert ctx is not None
    assert ctx.from_real_index is False
    assert ctx.citations == []


@pytest.mark.asyncio
async def test_falls_back_to_stub_snippet_when_no_indexed_chunks(db):
    ctx = await get_policy_context("returns_refunds", "some page text", SETTINGS, db=db, cache=None)
    assert ctx is not None
    assert ctx.from_real_index is False


@pytest.mark.asyncio
async def test_unknown_policy_id_with_no_stub_returns_none(db):
    ctx = await get_policy_context("not_a_real_policy_id", "text", SETTINGS, db=db, cache=None)
    assert ctx is None


@pytest.mark.asyncio
async def test_rebuild_index_scrapes_chunks_and_embeds(db):
    html = (
        "<html><body><main>"
        "<h2>Return window</h2><p>Items may be returned within thirty days of the delivery date for a full refund.</p>"
        "<h2>Refund method</h2><p>Refunds go back to the original payment method within five business days of receipt.</p>"
        "</main></body></html>"
    )
    with respx.mock:
        respx.get("https://example.org/returns-policy").mock(return_value=httpx.Response(200, text=html))
        with _mock_openai_embeddings(default_vector=[1.0, 0.0, 0.0]):
            count = await rebuild_policy_index("returns_refunds", ["https://example.org/returns-policy"], SETTINGS, db)

    assert count == 2  # two headed sections

    async with db.session() as session:
        from sqlalchemy import select
        rows = (await session.execute(select(PolicyChunk).where(PolicyChunk.policy_id == "returns_refunds"))).scalars().all()
    assert len(rows) == 2
    assert {r.section for r in rows} == {"Return window", "Refund method"}
    assert all(r.source_url == "https://example.org/returns-policy" for r in rows)
    assert all(json.loads(r.embedding_json) == [1.0, 0.0, 0.0] for r in rows)


@pytest.mark.asyncio
async def test_rebuild_index_replaces_old_chunks_not_appends(db):
    html_v1 = "<html><body><main><h2>Old</h2><p>Original policy content that is long enough to survive chunking.</p></main></body></html>"
    html_v2 = "<html><body><main><h2>New</h2><p>Updated policy content that is long enough to survive chunking too.</p></main></body></html>"

    with respx.mock:
        respx.get("https://example.org/policy").mock(return_value=httpx.Response(200, text=html_v1))
        with _mock_openai_embeddings():
            await rebuild_policy_index("misrepresentation", ["https://example.org/policy"], SETTINGS, db)

    with respx.mock:
        respx.get("https://example.org/policy").mock(return_value=httpx.Response(200, text=html_v2))
        with _mock_openai_embeddings():
            await rebuild_policy_index("misrepresentation", ["https://example.org/policy"], SETTINGS, db)

    async with db.session() as session:
        from sqlalchemy import select
        rows = (await session.execute(select(PolicyChunk).where(PolicyChunk.policy_id == "misrepresentation"))).scalars().all()
    sections = {r.section for r in rows}
    assert "Old" not in sections
    assert "New" in sections


@pytest.mark.asyncio
async def test_rebuild_index_with_no_api_key_is_a_noop(db):
    no_key_settings = Settings(openai_api_key="")
    count = await rebuild_policy_index("returns_refunds", ["https://example.org/whatever"], no_key_settings, db)
    assert count == 0


@pytest.mark.asyncio
async def test_retrieval_returns_top_n_by_similarity_with_real_citations(db):
    """Two very different chunks, two very different query vectors - the
    query closer to chunk A's vector should retrieve chunk A first.
    """
    html = (
        "<html><body><main>"
        "<h2>Shipping costs</h2><p>Shipping costs are calculated at checkout based on destination and package weight.</p>"
        "<h2>Prohibited items</h2><p>Weapons, counterfeit goods, and hazardous materials may not be listed for sale.</p>"
        "</main></body></html>"
    )
    with respx.mock:
        respx.get("https://example.org/policy").mock(return_value=httpx.Response(200, text=html))
        vectors = {
            "Shipping costs are calculated at checkout based on destination and package weight.": [1.0, 0.0],
            "Weapons, counterfeit goods, and hazardous materials may not be listed for sale.": [0.0, 1.0],
        }
        with _mock_openai_embeddings(vectors_by_text=vectors):
            await rebuild_policy_index("prohibited_content", ["https://example.org/policy"], SETTINGS, db)

        with _mock_openai_embeddings(default_vector=[0.0, 1.0]):
            ctx = await get_policy_context("prohibited_content", "query about weapons", SETTINGS, db, cache=None, top_n=1)

    assert ctx is not None
    assert ctx.from_real_index is True
    assert "Weapons" in ctx.summary
    assert "Shipping" not in ctx.summary
    assert "example.org/policy" in ctx.citations[0]
    assert "Prohibited items" in ctx.citations[0]


@pytest.mark.asyncio
async def test_retrieval_dedupes_citations_from_the_same_split_section(db):
    """A long section gets split into multiple chunks sharing the same
    (source_url, section) (see policy_chunker.py) - the top matches for a
    query shouldn't cite that one section 3 times just because it was long
    enough to split into 3 pieces.
    """
    long_sentence = "This is a real policy sentence about shipping costs and timing. "
    html = f"<html><body><main><h2>Shipping details</h2><p>{long_sentence * 30}</p></main></body></html>"
    with respx.mock:
        respx.get("https://example.org/policy").mock(return_value=httpx.Response(200, text=html))
        with _mock_openai_embeddings(default_vector=[1.0, 0.0]):
            count = await rebuild_policy_index("shipping_policy", ["https://example.org/policy"], SETTINGS, db)
    assert count > 1  # confirms this section really did split into multiple chunks

    with _mock_openai_embeddings(default_vector=[1.0, 0.0]):
        ctx = await get_policy_context("shipping_policy", "query", SETTINGS, db, cache=None, top_n=3)

    assert len(ctx.citations) == 1  # deduped down to the one distinct section, not 3 copies of it


@pytest.mark.asyncio
async def test_retrieval_query_embedding_is_cached(db):
    """The query text is typically the page being graded - an unchanged
    page's text should produce a cache hit on a repeat audit, same as LLM
    grading already does.
    """
    html = "<html><body><main><h2>Section</h2><p>Some real policy content long enough to survive chunking rules.</p></main></body></html>"
    with respx.mock:
        respx.get("https://example.org/policy").mock(return_value=httpx.Response(200, text=html))
        with _mock_openai_embeddings():
            await rebuild_policy_index("business_identity", ["https://example.org/policy"], SETTINGS, db)

    cache = LLMCache(db)
    with _mock_openai_embeddings() as mock_ctor:
        await get_policy_context("business_identity", "the same page text", SETTINGS, db, cache)
        await get_policy_context("business_identity", "the same page text", SETTINGS, db, cache)

    # rebuild_policy_index's chunk embedding didn't use this cache instance,
    # but the two identical retrieval queries here should only cost one
    # real embedding call between them.
    assert cache.hits == 1
    assert cache.misses == 1
