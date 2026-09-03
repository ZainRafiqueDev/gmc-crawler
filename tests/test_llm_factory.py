from app.config import Settings
from app.llm.claude_client import ClaudeClient
from app.llm.factory import get_llm_client
from app.llm.openai_client import OpenAIClient


def test_defaults_to_claude():
    settings = Settings(llm_provider="claude", anthropic_api_key="sk-ant-x")
    client = get_llm_client(settings)
    assert isinstance(client, ClaudeClient)


def test_llm_provider_openai_returns_openai_client():
    settings = Settings(llm_provider="openai", openai_api_key="sk-oa-x")
    client = get_llm_client(settings)
    assert isinstance(client, OpenAIClient)


def test_llm_configured_checks_the_right_key_per_provider():
    assert Settings(llm_provider="claude", anthropic_api_key="", openai_api_key="sk-oa-x").llm_configured is False
    assert Settings(llm_provider="claude", anthropic_api_key="sk-ant-x").llm_configured is True
    assert Settings(llm_provider="openai", openai_api_key="", anthropic_api_key="sk-ant-x").llm_configured is False
    assert Settings(llm_provider="openai", openai_api_key="sk-oa-x").llm_configured is True
