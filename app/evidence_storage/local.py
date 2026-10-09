"""Local-filesystem StorageAdapter - the zero-cloud-setup dev backend
(EVIDENCE_BACKEND=local, the default). Stores each blob as one file named by
its key directly under the configured base directory - no bucket/credentials
needed, works out of the box.
"""
from __future__ import annotations

from pathlib import Path


class LocalFileStorageAdapter:
    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: str) -> Path:
        # Keys are sha256 hex digests (64 hex chars) - no path-traversal
        # surface, but validated anyway since this takes a `key` argument
        # rather than only ever reading back a storage_key it issued itself.
        if "/" in key or ".." in key or not key:
            raise ValueError(f"invalid storage key: {key!r}")
        return self.base_dir / key

    def put(self, key: str, data: bytes, content_type: str) -> str:
        self._path_for(key).write_bytes(data)
        return key

    def get(self, storage_key: str) -> bytes:
        return self._path_for(storage_key).read_bytes()

    def delete(self, storage_key: str) -> None:
        self._path_for(storage_key).unlink(missing_ok=True)
