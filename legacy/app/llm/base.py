"""LLM provider abstraction.

The compliance engine's LLM track only ever calls `LLMProvider.complete()`.
It never knows or cares whether that's Ollama running locally or OpenAI's
API - provider-specific HTTP/auth/payload shape lives entirely inside each
adapter module.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class LLMProviderError(RuntimeError):
    """Raised on timeout, HTTP failure, or unparseable provider response."""


class LLMProvider(ABC):
    name: str = "unknown"

    @abstractmethod
    async def complete(self, *, system: str, user: str) -> str:
        """Return the raw text completion for a system+user prompt pair."""
        raise NotImplementedError
