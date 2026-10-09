"""Storage adapter interface for Tier 2 (cold) evidence blobs. Two
implementations (app.evidence_storage.local, app.evidence_storage.s3) -
callers (app.evidence_blob_store) only ever depend on this Protocol, so
swapping backends is a config change (EVIDENCE_BACKEND), never a code change.

Deliberately 3 methods, nothing more - no listing, no multi-part upload, no
presigned URLs. Keys are always the content hash (see Blob.hash in app.db),
so "does this key already exist" is answered by the `blobs` DB table, not by
asking the storage backend - these adapters never need a `head`/`exists` call.
"""
from __future__ import annotations

from typing import Protocol


class StorageAdapter(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> str:
        """Writes `data` under `key`, returns the storage_key to persist in
        Blob.storage_key (may differ from `key` for a backend that needs a
        prefix/namespace - callers must not assume storage_key == key)."""
        ...

    def get(self, storage_key: str) -> bytes:
        ...

    def delete(self, storage_key: str) -> None:
        """Must not raise if the object is already gone - the retention job
        calls this after a crash-recovery scenario could have left a DB row
        and a deleted object out of sync, and a missing object at delete time
        is not an error worth failing the whole prune pass over."""
        ...
