from __future__ import annotations

import pytest

from app.config import ConfigError, Settings


def test_nothing_set_runs_in_full_mock_mode(make_settings):
    settings = make_settings()
    assert settings.store_platform.value == "mock"
    assert settings.llm_provider.value == "ollama"
    assert settings.gmc_is_configured is False
    assert settings.store_is_live is False


def test_store_only_does_not_enable_gmc(make_settings):
    settings = make_settings(store_platform="woocommerce", store_url="https://store.example.com",
                              store_api_key="k", store_api_secret="s")
    assert settings.store_is_live is True
    assert settings.gmc_is_configured is False


def test_openai_key_switches_provider_with_no_explicit_llm_provider_set(make_settings):
    settings = make_settings(openai_api_key="sk-test")
    assert settings.llm_provider.value == "openai"


def test_full_config_enables_gmc(make_settings):
    settings = make_settings(
        store_platform="woocommerce", store_url="https://store.example.com",
        store_api_key="k", store_api_secret="s", openai_api_key="sk-test",
        gmc_merchant_id="12345", gmc_service_account_json_path="/tmp/sa.json",
    )
    assert settings.gmc_is_configured is True
    assert settings.llm_provider.value == "openai"


def test_malformed_config_llm_provider_openai_without_key_fails_fast(make_settings):
    with pytest.raises(ConfigError):
        make_settings(llm_provider="openai")


def test_malformed_config_woocommerce_without_store_url_fails_fast(make_settings):
    with pytest.raises(ConfigError):
        make_settings(store_platform="woocommerce")


def test_malformed_config_partial_gmc_config_fails_fast(make_settings):
    with pytest.raises(ConfigError):
        make_settings(gmc_merchant_id="12345")  # missing service account path
