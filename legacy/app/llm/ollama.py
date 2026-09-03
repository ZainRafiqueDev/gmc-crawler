from __future__ import annotations

import httpx

from app.llm.base import LLMProvider, LLMProviderError

DEFAULT_TIMEOUT_S = 30.0


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, host: str, model: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._host = host.rstrip("/")
        self._model = model
        self._timeout_s = timeout_s

    async def complete(self, *, system: str, user: str) -> str:
        payload = {
            "model": self._model,
            "prompt": user,
            "system": system,
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(f"{self._host}/api/generate", json=payload)
                resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise LLMProviderError(f"Ollama request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError(f"Ollama request failed: {exc}") from exc

        try:
            data = resp.json()
            return data["response"]
        except (ValueError, KeyError) as exc:
            raise LLMProviderError(f"Ollama returned an unparseable response: {exc}") from exc
