"""Selects GMC client + site-verification injector implementations from config."""
from __future__ import annotations

from app.config import Settings, StorePlatform
from app.gmc.auth import GoogleServiceAccountTokenProvider
from app.gmc.client import ContentAPIGMCClient, GMCClient, MockGMCClient
from app.gmc.site_verification import (
    MockSiteVerificationInjector,
    SiteVerificationInjector,
    WooCommerceSiteVerificationInjector,
)


def get_gmc_client(settings: Settings) -> GMCClient:
    if not settings.gmc_is_configured:
        return MockGMCClient()
    token_provider = GoogleServiceAccountTokenProvider(settings.gmc_service_account_json_path or "")
    return ContentAPIGMCClient(merchant_id=settings.gmc_merchant_id or "", access_token_provider=token_provider.get_token)


def get_site_verifier(settings: Settings) -> SiteVerificationInjector:
    if settings.store_platform == StorePlatform.WOOCOMMERCE and settings.store_url:
        return WooCommerceSiteVerificationInjector(
            store_url=settings.store_url,
            api_key=settings.store_api_key or "",
            api_secret=settings.store_api_secret or "",
        )
    return MockSiteVerificationInjector()
