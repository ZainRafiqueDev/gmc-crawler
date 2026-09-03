"""Goal 2.2 + Phase C: independent policy-update detection AND the trigger
for keeping the real RAG policy index (app/llm/policy_rag.py) fresh.
Re-fetches real GMC Help Center pages on their own schedule (decoupled from
any store's monitoring mode), hashes the combined visible content across
all of a policy area's source pages, and diffs against the last stored
hash. On the first check, or whenever that combined hash changes, the
policy area's chunks are re-scraped/re-chunked/re-embedded
(rebuild_policy_index) and every cached LLM grading result is invalidated
(LLMCache.invalidate_all) - a finding graded against stale policy text
shouldn't be silently trusted after the policy changes.

Each policy_id can have more than one real source URL: some GMC policy
areas (privacy_policy, terms_of_service) don't have one single dedicated
Help Center article the way shipping/returns do - the real guidance is
spread across a few pages instead. A change to ANY of a policy_id's URLs
counts as a change for that whole policy_id (combined-hash comparison), and
triggers a full re-index of all of that policy_id's URLs together.
"""
from __future__ import annotations

import logging

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel
from sqlalchemy import select

from app.change_detection import compute_content_hash
from app.config import Settings
from app.db import Database, PolicySourceSnapshot
from app.llm.cache import LLMCache
from app.llm.policy_rag import rebuild_policy_index
from app.security.ssrf_guard import GMC_AUDIT_USER_AGENT, safe_async_client

logger = logging.getLogger("gmc_audit.policy_watcher")

_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_HEADERS = {"User-Agent": GMC_AUDIT_USER_AGENT}

# policy_id (matches app/llm/policy_snippets.py) -> confirmed real GMC Help
# Center URL(s). Verified live as of this writing - re-check periodically,
# Google restructures Help Center URLs occasionally.
#
# shipping_policy, returns_refunds, business_identity, misrepresentation,
# and prohibited_content each have one dedicated, single-topic Help Center
# article. privacy_policy and terms_of_service do not - GMC's real guidance
# for these is spread across a few broader pages rather than one dedicated
# article for each, confirmed via live research plus the user's own
# pointers (in particular, "Fixing Merchant Center warnings and account
# suspensions" explicitly instructs merchants to "explicitly link to your
# delivery, refund and privacy policies" under its misrepresentation
# guidance - real evidence privacy-policy-linking is genuinely part of
# GMC's official guidance, just not as its own dedicated page).
POLICY_SOURCE_URLS: dict[str, list[str]] = {
    "shipping_policy": ["https://support.google.com/merchants/answer/6324484"],
    "returns_refunds": ["https://support.google.com/merchants/answer/15625417"],
    "business_identity": ["https://support.google.com/merchants/answer/17123687"],
    "misrepresentation": ["https://support.google.com/merchants/answer/6150127"],
    "prohibited_content": ["https://support.google.com/merchants/answer/6149970"],
    "editorial_quality": ["https://support.google.com/merchants/answer/12079604"],
    "privacy_policy": [
        "https://support.google.com/merchants/answer/9158778",
        "https://support.google.com/merchants/answer/6149970",
        "https://support.google.com/merchants/answer/13693195",
    ],
    "terms_of_service": [
        "https://support.google.com/merchants/checklist/16993969",
        "https://support.google.com/merchants/answer/13693195",
    ],
}


class PolicyChangeResult(BaseModel):
    policy_id: str
    source_urls: list[str]
    changed: bool
    is_first_check: bool
    previous_hash: str | None = None
    current_hash: str
    reindexed_chunk_count: int | None = None


async def _fetch_and_hash_combined(client: httpx.AsyncClient, urls: list[str]) -> str | None:
    """Fetches every URL for one policy_id and hashes their combined
    visible text as a single unit - a change to any one of them changes
    this hash. Returns None only if every URL failed to fetch (a partial
    fetch failure still produces a hash from whatever succeeded, since one
    temporarily-unreachable source among several shouldn't block detecting
    a real change on the others).
    """
    texts: list[str] = []
    for url in urls:
        try:
            resp = await client.get(url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Policy source fetch failed for %s: %s", url, exc)
            continue
        soup = BeautifulSoup(resp.text, "lxml")
        main = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup
        texts.append(main.get_text(separator=" ", strip=True))

    if not texts:
        return None
    return compute_content_hash("\n\n".join(texts))


async def check_policy_sources(
    db: Database, settings: Settings | None = None, cache: LLMCache | None = None,
) -> list[PolicyChangeResult]:
    """Fetches every mapped policy source, compares its combined hash
    against the last stored one, and records the new hash. Returns one
    result per policy_id - `changed=True` (and not `is_first_check`) means
    that policy area's real content actually differs from what was last
    observed.

    When settings is given (and an OpenAI key is configured), a first check
    or a detected change also triggers a real re-index of that policy_id's
    chunks (rebuild_policy_index) and invalidates the entire LLM cache -
    without settings, this only does change *detection*, matching the
    original Goal 2.2 scope (settings is optional so existing callers that
    only care about detection, and every existing test, keep working
    unchanged).
    """
    results: list[PolicyChangeResult] = []
    needs_reindex: list[tuple[str, list[str]]] = []

    # Phase 1: detect changes and update snapshots, all within one session -
    # committed and closed before any reindexing starts. rebuild_policy_index
    # opens its own independent session(s); calling it while this outer
    # session's transaction is still open causes real SQLite lock
    # contention ("database is locked") - verified live, not hypothetical.
    async with safe_async_client() as client:
        async with db.session() as session:
            for policy_id, urls in POLICY_SOURCE_URLS.items():
                current_hash = await _fetch_and_hash_combined(client, urls)
                if current_hash is None:
                    logger.warning("All source URLs failed for policy_id=%r - skipping this check", policy_id)
                    continue

                existing = (await session.execute(
                    select(PolicySourceSnapshot).where(PolicySourceSnapshot.policy_id == policy_id)
                )).scalar_one_or_none()

                is_first_check = existing is None
                changed = existing is not None and existing.content_hash != current_hash
                previous_hash = existing.content_hash if existing else None

                if existing is None:
                    session.add(PolicySourceSnapshot(policy_id=policy_id, source_url=urls[0], content_hash=current_hash))
                    logger.info("Policy source baseline recorded for %s", policy_id)
                elif changed:
                    logger.warning("Policy source CHANGED for %s (%s) - findings citing this policy may be stale", policy_id, urls)
                    existing.content_hash = current_hash
                else:
                    logger.info("Policy source unchanged for %s", policy_id)

                if settings is not None and (is_first_check or changed):
                    needs_reindex.append((policy_id, urls))

                results.append(PolicyChangeResult(
                    policy_id=policy_id, source_urls=urls, changed=changed,
                    is_first_check=is_first_check, previous_hash=previous_hash,
                    current_hash=current_hash, reindexed_chunk_count=None,
                ))

            await session.commit()

    # Phase 2: reindex, now that the phase-1 session is closed.
    reindexed_counts: dict[str, int] = {}
    for policy_id, urls in needs_reindex:
        reindexed_counts[policy_id] = await rebuild_policy_index(policy_id, urls, settings, db, cache)

    if reindexed_counts:
        for result in results:
            if result.policy_id in reindexed_counts:
                result.reindexed_chunk_count = reindexed_counts[result.policy_id]

    if any(reindexed_counts.values()) and cache is not None:
        n = await cache.invalidate_all()
        logger.warning("Policy index changed - invalidated %d cached LLM grading result(s)", n)

    return results
