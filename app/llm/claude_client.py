"""Thin wrapper around the Anthropic SDK, forcing structured (tool-use)
output for every LLM-graded check so results are parseable dicts, not
free-text the caller has to regex out.
"""
from __future__ import annotations

import logging

from anthropic import AsyncAnthropic

logger = logging.getLogger("gmc_audit.llm.claude_client")


class ClaudeClient:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self.model = model

    async def call_tool(self, system: str, user: str, tool_name: str, tool_schema: dict, max_tokens: int = 1024) -> dict | None:
        return await self._call_tool_with_content(system, [{"type": "text", "text": user}], tool_name, tool_schema, max_tokens)

    async def call_tool_with_image(
        self, system: str, user_text: str, image_url: str, tool_name: str, tool_schema: dict, max_tokens: int = 1024,
    ) -> dict | None:
        """Same as call_tool, but attaches an image by URL - Claude fetches
        it server-side, so callers never need to download/base64-encode
        images themselves.
        """
        content = [
            {"type": "image", "source": {"type": "url", "url": image_url}},
            {"type": "text", "text": user_text},
        ]
        return await self._call_tool_with_content(system, content, tool_name, tool_schema, max_tokens)

    async def _call_tool_with_content(self, system: str, content: list[dict], tool_name: str, tool_schema: dict, max_tokens: int) -> dict | None:
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": content}],
                tools=[{"name": tool_name, "description": f"Submit the {tool_name} result.", "input_schema": tool_schema}],
                tool_choice={"type": "tool", "name": tool_name},
            )
        except Exception as exc:  # noqa: BLE001 - any LLM failure must degrade to CANNOT_VERIFY, never crash the run
            logger.warning("Claude API call failed: %s", exc)
            return None

        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                return block.input
        logger.warning("Claude response had no %s tool_use block", tool_name)
        return None
