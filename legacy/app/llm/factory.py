"""Selects the active LLMProvider from config. The only place that does."""
from __future__ import annotations

from app.config import LLMProviderName, Settings
from app.llm.base import LLMProvider
from app.llm.ollama import OllamaProvider
from app.llm.openai_provider import OpenAIProvider


def get_llm_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == LLMProviderName.OPENAI:
        return OpenAIProvider(api_key=settings.openai_api_key or "")
    return OllamaProvider(host=settings.ollama_host, model=settings.ollama_model)
