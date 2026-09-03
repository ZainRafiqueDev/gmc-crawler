"""In-process fake provider - no HTTP at all.

Useful for tests/dev flows that want an LLM in the loop without caring
about provider wiring itself (that's what provider-parity tests are for).
Returns a canned "no violations found" JSON response unless a custom
responder is supplied.
"""
from __future__ import annotations

from collections.abc import Callable

from app.llm.base import LLMProvider

_DEFAULT_RESPONSE = '{"violations": []}'


class MockLLMProvider(LLMProvider):
    name = "mock"

    def __init__(self, responder: Callable[[str, str], str] | None = None) -> None:
        self._responder = responder

    async def complete(self, *, system: str, user: str) -> str:
        if self._responder is not None:
            return self._responder(system, user)
        return _DEFAULT_RESPONSE
