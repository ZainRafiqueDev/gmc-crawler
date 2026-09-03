"""Unit tests for the OpenAI LLMClient implementation. The OpenAI SDK is
mocked - these prove the forced-function-calling wiring and strict-schema
adaptation are correct, not that OpenAI's API itself behaves as documented
(that's what the live validation run is for).
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.llm.openai_client import OpenAIClient, _to_strict_schema

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "note": {"type": "string"}},
    "required": ["ok", "note"],
}


def _function_call_output(name: str, arguments: dict):
    item = MagicMock()
    item.type = "function_call"
    item.name = name
    item.arguments = json.dumps(arguments)
    return item


def _make_client_with_response(output_items):
    client = OpenAIClient(api_key="sk-test", model="gpt-4o")
    response = MagicMock()
    response.output = output_items
    client._client.responses.create = AsyncMock(return_value=response)
    return client


def test_to_strict_schema_adds_additional_properties_false():
    adapted = _to_strict_schema(SCHEMA)
    assert adapted["additionalProperties"] is False
    assert SCHEMA.get("additionalProperties") is None  # original untouched


def test_to_strict_schema_respects_existing_value():
    schema_with_flag = dict(SCHEMA, additionalProperties=True)
    adapted = _to_strict_schema(schema_with_flag)
    assert adapted["additionalProperties"] is True  # setdefault doesn't override


@pytest.mark.asyncio
async def test_call_tool_returns_parsed_arguments():
    client = _make_client_with_response([_function_call_output("submit_verdict", {"ok": True, "note": "fine"})])
    result = await client.call_tool("system prompt", "user text", "submit_verdict", SCHEMA)
    assert result == {"ok": True, "note": "fine"}

    call_kwargs = client._client.responses.create.call_args.kwargs
    assert call_kwargs["instructions"] == "system prompt"
    assert call_kwargs["tool_choice"] == {"type": "function", "name": "submit_verdict"}
    assert call_kwargs["tools"][0]["strict"] is True
    assert call_kwargs["tools"][0]["parameters"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_call_tool_with_image_sends_image_and_text_content():
    client = _make_client_with_response([_function_call_output("submit_verdict", {"ok": False, "note": "mismatch"})])
    result = await client.call_tool_with_image("system", "describe this", "https://example.com/img.jpg", "submit_verdict", SCHEMA)
    assert result == {"ok": False, "note": "mismatch"}

    call_kwargs = client._client.responses.create.call_args.kwargs
    content = call_kwargs["input"][0]["content"]
    assert content[0] == {"type": "input_image", "image_url": "https://example.com/img.jpg", "detail": "auto"}
    assert content[1] == {"type": "input_text", "text": "describe this"}


@pytest.mark.asyncio
async def test_returns_none_when_no_matching_function_call_in_output():
    client = _make_client_with_response([_function_call_output("some_other_tool", {"x": 1})])
    result = await client.call_tool("system", "user", "submit_verdict", SCHEMA)
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_arguments_not_valid_json():
    item = MagicMock()
    item.type = "function_call"
    item.name = "submit_verdict"
    item.arguments = "{not valid json"
    client = _make_client_with_response([item])
    result = await client.call_tool("system", "user", "submit_verdict", SCHEMA)
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_api_call_raises():
    client = OpenAIClient(api_key="sk-test", model="gpt-4o")
    client._client.responses.create = AsyncMock(side_effect=RuntimeError("network error"))
    result = await client.call_tool("system", "user", "submit_verdict", SCHEMA)
    assert result is None
