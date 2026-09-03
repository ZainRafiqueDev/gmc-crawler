"""OpenAI implementation of the LLMClient interface (app/llm/client.py),
using the Responses API's forced function-calling in strict Structured
Outputs mode - the OpenAI equivalent of Claude's forced tool-use. The
model has no way to return free-form prose instead of the schema: strict
mode rejects/repairs non-conforming output at the API layer, and
`tool_choice` forces this specific function to be called every time.
"""
from __future__ import annotations

import json
import logging

from openai import AsyncOpenAI

logger = logging.getLogger("gmc_audit.llm.openai_client")


def _to_strict_schema(schema: dict) -> dict:
    """OpenAI's strict Structured Outputs mode requires additionalProperties:
    false on every object level (Claude's tool_schema doesn't need this) -
    copy the schema rather than mutate the shared module-level constant the
    check functions pass in.
    """
    adapted = dict(schema)
    adapted.setdefault("additionalProperties", False)
    return adapted


class OpenAIClient:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = AsyncOpenAI(api_key=api_key)
        self.model = model

    async def call_tool(self, system: str, user: str, tool_name: str, tool_schema: dict, max_tokens: int = 1024) -> dict | None:
        content = [{"type": "input_text", "text": user}]
        return await self._call_tool_with_content(system, content, tool_name, tool_schema, max_tokens)

    async def call_tool_with_image(
        self, system: str, user_text: str, image_url: str, tool_name: str, tool_schema: dict, max_tokens: int = 1024,
    ) -> dict | None:
        content = [
            {"type": "input_image", "image_url": image_url, "detail": "auto"},
            {"type": "input_text", "text": user_text},
        ]
        return await self._call_tool_with_content(system, content, tool_name, tool_schema, max_tokens)

    async def _call_tool_with_content(self, system: str, content: list[dict], tool_name: str, tool_schema: dict, max_tokens: int) -> dict | None:
        try:
            response = await self._client.responses.create(
                model=self.model,
                instructions=system,
                input=[{"role": "user", "content": content}],
                max_output_tokens=max_tokens,
                tools=[{
                    "type": "function",
                    "name": tool_name,
                    "description": f"Submit the {tool_name} result.",
                    "parameters": _to_strict_schema(tool_schema),
                    "strict": True,
                }],
                tool_choice={"type": "function", "name": tool_name},
            )
        except Exception as exc:  # noqa: BLE001 - any LLM failure must degrade to CANNOT_VERIFY, never crash the run
            logger.warning("OpenAI API call failed: %s", exc)
            return None

        for item in response.output:
            if getattr(item, "type", None) == "function_call" and item.name == tool_name:
                try:
                    return json.loads(item.arguments)
                except (json.JSONDecodeError, TypeError) as exc:
                    logger.warning("OpenAI function_call arguments were not valid JSON: %s", exc)
                    return None

        logger.warning("OpenAI response had no %s function_call output", tool_name)
        return None
