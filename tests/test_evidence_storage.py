"""Tests for the Tier 2 evidence storage layer: compression round-trips (incl.
the zstd -> gzip fallback when zstandard isn't installed), the local
filesystem storage adapter, and write_blob_if_needed's dedup-on-write
behavior (identical content uploaded once, referenced twice).
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import Blob, Database
from app.evidence_blob_store import content_hash_for_blob, release_blob, write_blob_if_needed
from app.evidence_storage import compression
from app.evidence_storage.local import LocalFileStorageAdapter


def test_compress_decompress_roundtrip_each_codec():
    data = b"the quick brown fox jumps over the lazy dog" * 50
    for codec in ("zstd", "gzip", "none"):
        compressed = compression.compress(data, codec)
        assert compression.decompress(compressed, codec) == data


def test_resolve_codec_falls_back_to_gzip_when_zstandard_missing(monkeypatch):
    monkeypatch.setattr(compression, "zstandard", None)
    assert compression.resolve_codec("zstd") == "gzip"
    assert compression.resolve_codec("gzip") == "gzip"
    assert compression.resolve_codec("none") == "none"


def test_local_adapter_put_get_delete_roundtrip(tmp_path):
    adapter = LocalFileStorageAdapter(tmp_path / "evidence")
    key = content_hash_for_blob(b"hello world")

    storage_key = adapter.put(key, b"compressed-bytes", "text/html")
    assert storage_key == key
    assert adapter.get(key) == b"compressed-bytes"

    adapter.delete(key)
    assert not (tmp_path / "evidence" / key).exists()
    adapter.delete(key)  # must not raise on an already-deleted key


def test_local_adapter_rejects_path_traversal(tmp_path):
    adapter = LocalFileStorageAdapter(tmp_path / "evidence")
    with pytest.raises(ValueError):
        adapter.put("../escape", b"x", "text/plain")


@pytest.mark.asyncio
async def test_write_blob_if_needed_dedups_identical_content(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/blob_test.db")
    await db.init()
    adapter = LocalFileStorageAdapter(tmp_path / "evidence")
    settings = Settings(evidence_compression="gzip")

    body = b"<html>same page content</html>"

    async with db.session() as session:
        blob1 = await write_blob_if_needed(session, adapter, settings, body, "text/html")
        await session.commit()

    async with db.session() as session:
        blob2 = await write_blob_if_needed(session, adapter, settings, body, "text/html")
        await session.commit()

    assert blob1.hash == blob2.hash

    async with db.session() as session:
        rows = (await session.execute(select(Blob))).scalars().all()
    assert len(rows) == 1  # one upload, not two
    assert rows[0].refcount == 2  # referenced twice

    await db.dispose()


@pytest.mark.asyncio
async def test_release_blob_deletes_row_and_object_at_zero_refcount(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/blob_release_test.db")
    await db.init()
    adapter = LocalFileStorageAdapter(tmp_path / "evidence")
    settings = Settings(evidence_compression="none")

    async with db.session() as session:
        blob = await write_blob_if_needed(session, adapter, settings, b"body", "text/html")
        await session.commit()
        blob_hash = blob.hash

    assert adapter.get(blob_hash) == b"body"

    async with db.session() as session:
        await release_blob(session, adapter, blob_hash)
        await session.commit()

    async with db.session() as session:
        assert await session.get(Blob, blob_hash) is None
    with pytest.raises(FileNotFoundError):
        adapter.get(blob_hash)

    await db.dispose()
