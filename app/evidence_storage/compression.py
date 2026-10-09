"""Compression for Tier 2 evidence blobs (HTML/clean-text/JSON-LD) -
compressed in the app before upload, never left to the storage backend. No
custom format: just zstd or stdlib gzip, with the codec actually used
recorded in Blob.codec so a reader always knows how to decompress regardless
of what EVIDENCE_COMPRESSION says *now*.
"""
from __future__ import annotations

import gzip
import logging

logger = logging.getLogger("gmc_audit.evidence_storage.compression")

try:
    import zstandard
except ImportError:  # pragma: no cover - zstandard is a transitive dep today, but not guaranteed
    zstandard = None


def resolve_codec(requested: str) -> str:
    """Falls back to gzip (always available, stdlib) if zstd was requested
    but the zstandard package isn't installed - logged once per call rather
    than failing evidence writes over an optional dependency."""
    if requested == "zstd" and zstandard is None:
        logger.warning("evidence_compression=zstd requested but the zstandard package isn't installed - falling back to gzip")
        return "gzip"
    return requested


def compress(data: bytes, codec: str) -> bytes:
    if codec == "zstd":
        return zstandard.ZstdCompressor().compress(data)
    if codec == "gzip":
        return gzip.compress(data)
    if codec == "none":
        return data
    raise ValueError(f"unknown codec: {codec!r}")


def decompress(data: bytes, codec: str) -> bytes:
    if codec == "zstd":
        return zstandard.ZstdDecompressor().decompress(data)
    if codec == "gzip":
        return gzip.decompress(data)
    if codec == "none":
        return data
    raise ValueError(f"unknown codec: {codec!r}")
