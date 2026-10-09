from __future__ import annotations

from app.config import Settings
from app.evidence_storage.base import StorageAdapter
from app.evidence_storage.local import LocalFileStorageAdapter


def get_storage_adapter(settings: Settings) -> StorageAdapter:
    if settings.evidence_backend == "s3":
        from app.evidence_storage.s3 import S3StorageAdapter
        return S3StorageAdapter(settings)
    return LocalFileStorageAdapter(settings.evidence_bucket)
