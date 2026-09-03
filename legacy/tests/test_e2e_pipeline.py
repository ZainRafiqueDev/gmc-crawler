"""Full end-to-end pipeline tests, one per config state (build brief's
E2E-1..E2E-5). Each exercises `build_pipeline(settings).run_once()` for
real — only the external HTTP layer is mocked via respx. No internal
function is stubbed out.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx

from app.pipeline import build_pipeline

FIXTURES = Path(__file__).parent / "fixtures"
WC_MIXED = json.loads((FIXTURES / "woocommerce_products.json").read_text())
WC_CLEAN = json.loads((FIXTURES / "woocommerce_products_clean.json").read_text())
WC_ONE_CRITICAL = json.loads((FIXTURES / "woocommerce_products_one_critical.json").read_text())

OLLAMA_URL = "http://localhost:11434/api/generate"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
STORE_URL = "https://store.example.com"
STORE_PRODUCTS_URL = f"{STORE_URL}/wp-json/wc/v3/products"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMC_VERIFY_URL = "https://www.googleapis.com/siteVerification/v1/token"
STORE_VERIFY_INJECT_URL = f"{STORE_URL}/wp-json/gmc-compliance/v1/site-verification"
GMC_MERCHANT_ID = "999888777"
GMC_CLAIM_URL = f"https://www.googleapis.com/content/v2.1/{GMC_MERCHANT_ID}/accounts/{GMC_MERCHANT_ID}/claimwebsite"
GMC_FEED_URL = f"https://www.googleapis.com/content/v2.1/{GMC_MERCHANT_ID}/products/batch"

CLEAN_LLM_RESPONSE = json.dumps({"violations": []})


def _mock_ollama():
    return respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json={"response": CLEAN_LLM_RESPONSE}))


def _mock_openai():
    return respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": CLEAN_LLM_RESPONSE}}]})
    )


def _mock_gmc_success():
    respx.post(GOOGLE_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
    )
    respx.post(GMC_VERIFY_URL).mock(return_value=httpx.Response(200, json={"token": "gmc-verify-tok-e2e"}))
    respx.post(STORE_VERIFY_INJECT_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    respx.get(STORE_URL).mock(
        return_value=httpx.Response(
            200, text='<html><head><meta name="google-site-verification" '
                       'content="gmc-verify-tok-e2e" /></head><body>ok</body></html>',
        )
    )
    claim_route = respx.post(GMC_CLAIM_URL).mock(return_value=httpx.Response(200, json={}))
    feed_route = respx.post(GMC_FEED_URL).mock(return_value=httpx.Response(200, json={"kind": "batch"}))
    return claim_route, feed_route


@respx.mock
async def test_e2e_1_nothing_set_runs_mock_catalog_reports_and_skips_gmc(make_settings, caplog):
    _mock_ollama()
    settings = make_settings()
    pipeline = build_pipeline(settings)

    caplog.set_level("INFO")
    result = await pipeline.run_once()

    assert result.skipped is False
    assert result.report is not None
    assert len(result.report.product_results) == 12
    assert result.connect_result is None
    assert pipeline.gate is None  # no GMC client was ever constructed, let alone called
    assert any("GMC not configured" in r.message for r in caplog.records)


@respx.mock
async def test_e2e_2_store_only_pulls_real_catalog_and_still_skips_gmc(make_settings):
    _mock_ollama()
    respx.get(STORE_PRODUCTS_URL).mock(return_value=httpx.Response(200, json=WC_MIXED))
    settings = make_settings(store_platform="woocommerce", store_url=STORE_URL,
                              store_api_key="ck", store_api_secret="cs")
    pipeline = build_pipeline(settings)

    result = await pipeline.run_once()

    assert result.skipped is False
    assert len(result.report.product_results) == 2
    assert result.connect_result is None
    assert pipeline.gate is None


@respx.mock
async def test_e2e_3_store_plus_llm_key_uses_openai_not_ollama(make_settings):
    openai_route = _mock_openai()
    ollama_route = respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json={"response": CLEAN_LLM_RESPONSE}))
    respx.get(STORE_PRODUCTS_URL).mock(return_value=httpx.Response(200, json=WC_MIXED))
    settings = make_settings(store_platform="woocommerce", store_url=STORE_URL,
                              store_api_key="ck", store_api_secret="cs", openai_api_key="sk-test")
    pipeline = build_pipeline(settings)

    result = await pipeline.run_once()

    assert openai_route.called
    assert not ollama_route.called
    assert len(result.report.product_results) == 2  # same report shape as E2E-2
    assert result.connect_result is None


@respx.mock
async def test_e2e_4_store_llm_gmc_clean_catalog_full_connect_flow(make_settings, fake_service_account_path):
    _mock_ollama()
    respx.get(STORE_PRODUCTS_URL).mock(return_value=httpx.Response(200, json=WC_CLEAN))
    claim_route, feed_route = _mock_gmc_success()

    settings = make_settings(
        store_platform="woocommerce", store_url=STORE_URL, store_api_key="ck", store_api_secret="cs",
        gmc_merchant_id=GMC_MERCHANT_ID, gmc_service_account_json_path=fake_service_account_path,
    )
    pipeline = build_pipeline(settings)

    result = await pipeline.run_once()

    assert result.report.is_clean is True
    assert result.connect_result is not None
    assert result.connect_result.connected is True
    assert claim_route.called
    assert feed_route.called


@respx.mock
async def test_e2e_5_store_llm_gmc_one_critical_violation_never_connects(make_settings, fake_service_account_path):
    _mock_ollama()
    respx.get(STORE_PRODUCTS_URL).mock(return_value=httpx.Response(200, json=WC_ONE_CRITICAL))
    claim_route, feed_route = _mock_gmc_success()

    settings = make_settings(
        store_platform="woocommerce", store_url=STORE_URL, store_api_key="ck", store_api_secret="cs",
        gmc_merchant_id=GMC_MERCHANT_ID, gmc_service_account_json_path=fake_service_account_path,
    )
    pipeline = build_pipeline(settings)

    result = await pipeline.run_once()

    assert result.report.is_clean is False
    assert result.report.critical_count >= 1
    assert result.connect_result is not None
    assert result.connect_result.connected is False
    assert not claim_route.called
    assert not feed_route.called
