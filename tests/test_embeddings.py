"""Unit tests for the OpenAI embeddings wrapper (Phase C). The OpenAI SDK
is mocked - these prove the caching wiring is correct, not that OpenAI's
embeddings API itself behaves as documented.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings
from app.db import Database
from app.llm.cache import LLMCache
from app.llm.embeddings import embed_text

SETTINGS = Settings(openai_api_key="sk-test", openai_embedding_model="text-embedding-3-small")


def _mock_openai_returning(vector: list[float]):
    response = MagicMock()
    response.data = [MagicMock(embedding=vector)]
    mock_client = MagicMock()
    mock_client.embeddings.create = AsyncMock(return_value=response)
    return patch("app.llm.embeddings.AsyncOpenAI", return_value=mock_client)


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await database.init()
    yield database
    await database.dispose()


@pytest.mark.asyncio
async def test_embed_text_returns_the_vector_from_openai():
    with _mock_openai_returning([0.1, 0.2, 0.3]) as mock_ctor:
        result = await embed_text("some policy text", SETTINGS)
    assert result == [0.1, 0.2, 0.3]
    mock_ctor.assert_called_once()


@pytest.mark.asyncio
async def test_embed_text_is_cached_on_second_call_with_same_content(db):
    cache = LLMCache(db)
    with _mock_openai_returning([0.5, 0.5]) as mock_ctor:
        first = await embed_text("same text", SETTINGS, cache)
        second = await embed_text("same text", SETTINGS, cache)
    assert first == second == [0.5, 0.5]
    mock_ctor.assert_called_once()  # second call was a cache hit, no new OpenAI call
    assert cache.hits == 1
    assert cache.misses == 1


@pytest.mark.asyncio
async def test_embed_text_different_content_is_not_cached_together(db):
    cache = LLMCache(db)
    with patch("app.llm.embeddings.AsyncOpenAI") as mock_ctor:
        def make_response(vec):
            response = MagicMock()
            response.data = [MagicMock(embedding=vec)]
            return response

        mock_client = MagicMock()
        mock_client.embeddings.create = AsyncMock(side_effect=[make_response([1.0]), make_response([2.0])])
        mock_ctor.return_value = mock_client

        first = await embed_text("text A", SETTINGS, cache)
        second = await embed_text("text B", SETTINGS, cache)

    assert first == [1.0]
    assert second == [2.0]
    assert mock_client.embeddings.create.call_count == 2
