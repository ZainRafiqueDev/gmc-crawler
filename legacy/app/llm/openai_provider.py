from __future__ import annotations

import httpx

from app.llm.base import LLMProvider, LLMProviderError

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MODEL = "gpt-4o-mini"
API_URL = "https://api.openai.com/v1/chat/completions"


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout_s = timeout_s

    async def complete(self, *, system: str, user: str) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(API_URL, json=payload, headers=headers)
                resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise LLMProviderError(f"OpenAI request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError(f"OpenAI request failed: {exc}") from exc

        try:
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMProviderError(f"OpenAI returned an unparseable response: {exc}") from exc
