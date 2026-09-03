"""Picks the LLM provider based on config. Every caller in app/llm/ and
app/checks/*.py gets an LLMClient (app/llm/client.py) from here - never
imports ClaudeClient/OpenAIClient directly - so switching providers is a
config change (LLM_PROVIDER=claude|openai), not a code change.
"""
from __future__ import annotations

from app.config import Settings
from app.llm.cache import CachedLLMClient, LLMCache
from app.llm.claude_client import ClaudeClient
from app.llm.client import LLMClient
from app.llm.openai_client import OpenAIClient


def get_llm_client(settings: Settings, cache: LLMCache | None = None) -> LLMClient:
    if settings.llm_provider == "openai":
        model = settings.openai_model
        client: LLMClient = OpenAIClient(settings.openai_api_key, model)
    else:
        model = settings.anthropic_model
        client = ClaudeClient(settings.anthropic_api_key, model)

    if cache is not None:
        return CachedLLMClient(client, cache, provider=settings.llm_provider, model=model)
    return client
