"""Resilience: isolated failures shouldn't crash a run, repeat runs
shouldn't duplicate alerts, and malformed config should fail fast at
startup rather than mid-run."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.config import ConfigError
from app.pipeline import build_pipeline

OLLAMA_URL = "http://localhost:11434/api/generate"
STORE_URL = "https://store.example.com"
STORE_PRODUCTS_URL = f"{STORE_URL}/wp-json/wc/v3/products"
CLEAN_LLM_RESPONSE = json.dumps({"violations": []})


@respx.mock
async def test_llm_timeout_flags_product_manual_review_and_pipeline_keeps_running(make_settings):
    respx.post(OLLAMA_URL).mock(side_effect=httpx.TimeoutException("timed out"))
    settings = make_settings()
    pipeline = build_pipeline(settings)

    result = await pipeline.run_once()

    assert result.skipped is False
    assert result.report is not None
    assert len(result.report.product_results) == 12
    assert all(r.needs_manual_review for r in result.report.product_results)


@respx.mock
async def test_store_api_error_isolated_failure_not_full_crash(make_settings):
    respx.get(STORE_PRODUCTS_URL).mock(return_value=httpx.Response(503, text="rate limited"))
    settings = make_settings(store_platform="woocommerce", store_url=STORE_URL,
                              store_api_key="ck", store_api_secret="cs")
    pipeline = build_pipeline(settings)

    result = await pipeline.run_once()  # must not raise

    assert result.skipped is True
    assert result.report is None


@respx.mock
async def test_repeat_run_same_catalog_does_not_duplicate_alerts_at_pipeline_level(make_settings):
    respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json={"response": CLEAN_LLM_RESPONSE}))
    settings = make_settings()  # mock catalog has known critical violations every run
    pipeline = build_pipeline(settings)

    await pipeline.run_once()
    await pipeline.run_once()

    alerts = pipeline.dashboard_store.list_alerts()
    assert len(alerts) == 1


def test_malformed_ollama_host_fails_at_startup_with_clear_error(make_settings):
    with pytest.raises(ConfigError, match="OLLAMA_HOST"):
        make_settings(ollama_host="not-a-valid-host")
