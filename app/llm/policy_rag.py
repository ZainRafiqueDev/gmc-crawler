"""Phase C: real RAG retrieval over live-scraped GMC policy pages, replacing
the hand-written stub summaries in app/llm/policy_snippets.py as the
grounding text every LLM-graded check cites.

Storage: chunk text + embedding live in the policy_chunks table as a plain
JSON-encoded float list (app/db.py::PolicyChunk), not a native pgvector
column. The corpus here is a few hundred chunks at most (8 policy areas x a
handful of real source pages each), so a full Python-side cosine-similarity
scan over one policy_id's chunks is effectively instant; a real ANN vector
index buys nothing at this scale and would add a hard Postgres+pgvector-
extension dependency this project doesn't otherwise need (SQLite stays the
zero-setup default everywhere else). If the corpus ever grows enough that
this stops being true, swapping this module's storage/query for
pgvector.sqlalchemy.Vector + a real similarity index is a contained change -
nothing outside this module needs to know its internals.

Retrieval is genuinely dynamic, not a fixed canonical blurb: the query
embedded for each check is the actual page text being graded, so two
different pages checked against the same policy_id can surface different
top-N chunks - e.g. a page mentioning a specific return window can retrieve
the chunk about return windows specifically, not just a generic returns
overview. This is also cache-friendly the same way grading already is: an
unchanged page's text produces the same query, so the query embedding is a
cache hit on a repeat audit.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime

import httpx
from pydantic import BaseModel
from sqlalchemy import delete, select

from app.config import Settings
from app.db import Database, PolicyChunk
from app.llm.cache import LLMCache
from app.llm.embeddings import embed_text
from app.llm.policy_chunker import chunk_html_by_headings
from app.llm.policy_snippets import PolicySnippet, get_snippet
from app.security.ssrf_guard import GMC_AUDIT_USER_AGENT, safe_async_client

logger = logging.getLogger("gmc_audit.llm.policy_rag")

TOP_N_DEFAULT = 3
_QUERY_TEXT_LIMIT = 2000
_FETCH_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_HEADERS = {"User-Agent": GMC_AUDIT_USER_AGENT}


class PolicyContext(BaseModel):
    id: str
    title: str
    summary: str
    citations: list[str]
    # False whenever retrieval fell back to the hand-written stub (no index
    # built yet, index empty, or the embedding call itself failed) - kept
    # visible rather than silently indistinguishable from a real hit.
    from_real_index: bool
    # When the cited chunk(s) were last (re-)scraped from the live GMC page -
    # PolicyChunk.created_at, set only when rebuild_policy_index actually
    # ran (a first check, or app.policy_watcher.check_policy_sources
    # detecting a real change - see that module). The OLDEST created_at
    # among the chunks actually cited, not the newest: if a citation
    # combines chunks verified at different times, the reader should see
    # the more conservative (staler) date, not the more recent one masking
    # it. None when from_real_index is False (nothing was actually
    # retrieved from a dated source to report a date for).
    verified_at: datetime | None = None


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _fallback(policy_id: str, title: str, snippet: PolicySnippet | None) -> PolicyContext | None:
    if snippet is None:
        return None
    return PolicyContext(id=policy_id, title=title, summary=snippet.summary, citations=[], from_real_index=False)


async def get_policy_context(
    policy_id: str, query_text: str, settings: Settings, db: Database | None, cache: LLMCache | None,
    top_n: int = TOP_N_DEFAULT,
) -> PolicyContext | None:
    """Retrieves the top_n most relevant real policy chunks for policy_id,
    using query_text (the page being checked) as the retrieval query. Falls
    back to the stub snippet - logged as a warning, since it shouldn't
    normally happen once the index is built - if there's no DB, no OpenAI
    key, no indexed chunks for this policy_id, or the embedding call fails.
    """
    snippet = get_snippet(policy_id)
    title = snippet.title if snippet else policy_id

    if db is None or not settings.openai_api_key:
        return _fallback(policy_id, title, snippet)

    async with db.session() as session:
        chunks = (await session.execute(
            select(PolicyChunk).where(PolicyChunk.policy_id == policy_id)
        )).scalars().all()

    if not chunks:
        logger.warning("No indexed policy chunks for policy_id=%r - falling back to stub snippet", policy_id)
        return _fallback(policy_id, title, snippet)

    try:
        query_embedding = await embed_text((query_text or title)[:_QUERY_TEXT_LIMIT], settings, cache)
    except Exception as exc:  # noqa: BLE001 - embedding failure must degrade, not crash a check
        logger.warning("Embedding the retrieval query failed for policy_id=%r: %s - falling back to stub snippet", policy_id, exc)
        return _fallback(policy_id, title, snippet)

    scored = sorted(
        chunks,
        key=lambda c: _cosine_similarity(query_embedding, json.loads(c.embedding_json)),
        reverse=True,
    )

    # A long section can be split across several chunks (see
    # policy_chunker.py) sharing the same (source_url, section) - dedupe to
    # that pair so citing "the same section 3 times" doesn't happen just
    # because it was long enough to split. Keeps only the single
    # highest-scoring piece of each section, not all of them.
    top: list[PolicyChunk] = []
    seen_sections: set[tuple[str, str]] = set()
    for chunk in scored:
        key = (chunk.source_url, chunk.section)
        if key in seen_sections:
            continue
        seen_sections.add(key)
        top.append(chunk)
        if len(top) >= top_n:
            break

    summary = "\n\n".join(f'[Source: {c.source_url} - "{c.section}"]\n{c.chunk_text}' for c in top)
    citations = [f'{c.source_url} ("{c.section}")' for c in top]
    verified_at = min(c.created_at for c in top)
    return PolicyContext(
        id=policy_id, title=title, summary=summary, citations=citations,
        from_real_index=True, verified_at=verified_at,
    )


async def rebuild_policy_index(
    policy_id: str, urls: list[str], settings: Settings, db: Database, cache: LLMCache | None = None,
) -> int:
    """(Re-)scrapes every source URL for one policy_id, chunks each page,
    embeds every chunk, and replaces that policy_id's stored chunks
    entirely - old chunks for this policy_id are deleted first. Simplest
    correct approach at this corpus size; no incremental chunk-level
    diffing. Returns how many chunks were stored (0 if every URL failed to
    fetch, or settings.openai_api_key isn't set).
    """
    if not settings.openai_api_key:
        logger.warning("Skipping policy index build for %r - no OPENAI_API_KEY configured", policy_id)
        return 0

    scraped: list[tuple[str, str, str]] = []  # (source_url, section, text)
    async with safe_async_client(timeout=_FETCH_TIMEOUT) as client:
        for url in urls:
            try:
                resp = await client.get(url, headers=_HEADERS, follow_redirects=True)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("Could not fetch %s while indexing policy_id=%r: %s", url, policy_id, exc)
                continue
            for section, text in chunk_html_by_headings(resp.text):
                scraped.append((url, section, text))

    if not scraped:
        logger.warning("Policy index build for %r produced 0 chunks (every source URL failed) - keeping any existing index", policy_id)
        return 0

    # Embed everything *before* opening the write session - embed_text's
    # cache lookups open their own session(s), and doing that while this
    # session's transaction is still open causes real SQLite lock
    # contention ("database is locked") - verified live, not hypothetical.
    embeddings = [await embed_text(text, settings, cache) for _, _, text in scraped]

    async with db.session() as session:
        await session.execute(delete(PolicyChunk).where(PolicyChunk.policy_id == policy_id))
        for i, ((url, section, text), embedding) in enumerate(zip(scraped, embeddings)):
            session.add(PolicyChunk(
                policy_id=policy_id, source_url=url, section=section,
                chunk_index=i, chunk_text=text, embedding_json=json.dumps(embedding),
            ))
        await session.commit()

    logger.info("Rebuilt policy index for %r: %d chunk(s) from %d source URL(s)", policy_id, len(scraped), len(urls))
    return len(scraped)
