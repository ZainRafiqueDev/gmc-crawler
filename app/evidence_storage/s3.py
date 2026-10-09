"""S3-compatible StorageAdapter (EVIDENCE_BACKEND=s3) - works against real
AWS S3 or any S3-compatible endpoint (Cloudflare R2, MinIO, etc.) by setting
evidence_s3_endpoint_url. boto3 is imported lazily, inside __init__, so a
local-only dev setup that never selects EVIDENCE_BACKEND=s3 never needs
boto3 installed at all.
"""
from __future__ import annotations

from app.config import Settings


class S3StorageAdapter:
    def __init__(self, settings: Settings) -> None:
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError(
                "EVIDENCE_BACKEND=s3 requires the boto3 package (pip install boto3)"
            ) from exc

        self.bucket = settings.evidence_bucket
        client_kwargs: dict = {"region_name": settings.evidence_s3_region}
        if settings.evidence_s3_endpoint_url:
            client_kwargs["endpoint_url"] = settings.evidence_s3_endpoint_url
        if settings.evidence_s3_access_key_id:
            client_kwargs["aws_access_key_id"] = settings.evidence_s3_access_key_id
            client_kwargs["aws_secret_access_key"] = settings.evidence_s3_secret_access_key
        self._client = boto3.client("s3", **client_kwargs)

    def put(self, key: str, data: bytes, content_type: str) -> str:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        return key

    def get(self, storage_key: str) -> bytes:
        return self._client.get_object(Bucket=self.bucket, Key=storage_key)["Body"].read()

    def delete(self, storage_key: str) -> None:
        # S3 DeleteObject is idempotent - no error on a missing key, matching
        # StorageAdapter.delete's "must not raise if already gone" contract.
        self._client.delete_object(Bucket=self.bucket, Key=storage_key)
