"""Section 4.2: per-key (IP/account) rate limiting so a public frontend's
audit-trigger endpoint can't be cost-bombed - each audit costs real LLM/
vision API money. In-memory sliding window, fine for a single-process
deployment (same scale assumption as APScheduler elsewhere in this
project); swap the storage for Redis if this ever runs multi-process.

Not wired into an HTTP endpoint yet - that happens when the frontend/API
layer is built. This is the reusable component + its test coverage.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        """Returns True and records the hit if under the limit; returns
        False (and does NOT record it) if the key is already at the limit
        within the current window.
        """
        now = time.monotonic()
        async with self._lock:
            hits = self._hits[key]
            cutoff = now - self.window_seconds
            while hits and hits[0] < cutoff:
                hits.popleft()
            if len(hits) >= self.max_requests:
                return False
            hits.append(now)
            return True

    async def remaining(self, key: str) -> int:
        now = time.monotonic()
        async with self._lock:
            hits = self._hits[key]
            cutoff = now - self.window_seconds
            while hits and hits[0] < cutoff:
                hits.popleft()
            return max(0, self.max_requests - len(hits))

    async def reset(self, key: str) -> None:
        async with self._lock:
            self._hits.pop(key, None)
