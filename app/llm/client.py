"""Provider-agnostic LLM client interface. Every check in app/llm/ and
app/checks/*.py that talks to an LLM only ever calls through this shape -
`app/llm/factory.py` decides which concrete client (Claude or OpenAI) to
hand back based on `Settings.llm_provider`, so the check logic itself never
imports a provider SDK directly.

Both `call_tool` and `call_tool_with_image` return the parsed structured
output as a plain dict, or None if the call failed/was unparseable - never
free-form text. Every implementation must force the model to return exactly
this schema (Claude: forced tool-use; OpenAI: forced function-calling with
strict Structured Outputs) rather than relying on prompting alone.
"""
from __future__ import annotations

from typing import Protocol


class LLMClient(Protocol):
    async def call_tool(self, system: str, user: str, tool_name: str, tool_schema: dict, max_tokens: int = 1024) -> dict | None: ...

    async def call_tool_with_image(
        self, system: str, user_text: str, image_url: str, tool_name: str, tool_schema: dict, max_tokens: int = 1024,
    ) -> dict | None: ...
