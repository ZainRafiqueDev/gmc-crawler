"""Dedup-on-write for Tier 2 evidence blobs: the content hash IS the storage
key (spec section, "Content-addressable + dedup"). Every write goes through
write_blob_if_needed, which never uploads the same content twice - a
resource whose content is byte-identical to one already stored (its own
previous run, or another resource entirely) gets a cheap DB-only reference
bump instead of a second upload.

This module only ever writes/reads the `blobs` table + the storage backend;
it never touches Resource/FindingRecord's own *_blob_hash columns - callers
(app.evidence_store, app.first_audit, future rule-engine code) set those
after getting a Blob back from here.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import Blob
from app.evidence_storage.base import StorageAdapter
from app.evidence_storage.compression import compress, resolve_codec


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def content_hash_for_blob(data: bytes) -> str:
    """Hashes the UNCOMPRESSED bytes - dedup must key on actual content, not
    on the compressed representation (same content compressed with a
    different codec, e.g. after an EVIDENCE_COMPRESSION config change, must
    still be recognized as a duplicate)."""
    return hashlib.sha256(data).hexdigest()


async def write_blob_if_needed(
    session: AsyncSession, adapter: StorageAdapter, settings: Settings, data: bytes, content_type: str,
) -> Blob:
    """Returns the Blob row for `data`'s content hash - uploads it (and
    inserts the row) only if this exact content has never been stored
    before; otherwise bumps refcount/last_referenced_at on the existing row
    and uploads nothing. Caller commits.
    """
    content_key = content_hash_for_blob(data)

    existing = await session.get(Blob, content_key)
    if existing is not None:
        existing.refcount += 1
        existing.last_referenced_at = _utcnow()
        return existing

    codec = resolve_codec(settings.evidence_compression)
    compressed = compress(data, codec)
    storage_key = adapter.put(content_key, compressed, content_type)

    blob = Blob(
        hash=content_key, storage_key=storage_key, content_type=content_type,
        bytes=len(compressed), codec=codec, refcount=1,
    )
    session.add(blob)
    await session.flush()
    return blob


async def release_blob(session: AsyncSession, adapter: StorageAdapter, blob_hash: str) -> None:
    """Decrements refcount; deletes the DB row and the stored object once it
    reaches zero. Safe to call on a hash that's already gone (no-op)."""
    blob = await session.get(Blob, blob_hash)
    if blob is None:
        return

    blob.refcount -= 1
    if blob.refcount <= 0:
        adapter.delete(blob.storage_key)
        await session.delete(blob)
