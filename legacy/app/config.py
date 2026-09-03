"""Central, config-driven runtime settings.

Nothing downstream should branch on "is this real or mocked" directly -
it should ask `Settings` (via the properties below) or receive an already
-selected implementation from a factory. This is the one file allowed to
know that env vars drive mock-vs-live behavior.
"""
from __future__ import annotations

import logging
import sys
from enum import Enum

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("gmc_compliance.config")


class StorePlatform(str, Enum):
    MOCK = "mock"
    WOOCOMMERCE = "woocommerce"
    SHOPIFY = "shopify"


class LLMProviderName(str, Enum):
    OLLAMA = "ollama"
    OPENAI = "openai"


class ConfigError(RuntimeError):
    """Raised at startup when config is present but internally inconsistent."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Store
    store_platform: StorePlatform = StorePlatform.MOCK
    store_url: str | None = None
    store_api_key: str | None = None
    store_api_secret: str | None = None

    # LLM
    llm_provider: LLMProviderName = LLMProviderName.OLLAMA
    openai_api_key: str | None = None
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "mistral"

    # GMC
    gmc_merchant_id: str | None = None
    gmc_service_account_json_path: str | None = None

    # Notifications
    alert_email_to: str | None = None
    resend_api_key: str | None = None

    # API auth — protects /scan and /dashboard/alerts, which can expose real
    # catalog/violation data and (once GMC is configured) trigger a live
    # claimwebsite call. Blank is fine for pure local/mock development; set
    # it before this is reachable from anywhere but localhost.
    api_auth_token: str | None = None

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/gmc_compliance"

    @model_validator(mode="after")
    def _validate_consistency(self) -> "Settings":
        # Effective LLM provider follows the documented default: OPENAI_API_KEY
        # present flips the fuzzy-check track to GPT even if LLM_PROVIDER wasn't
        # explicitly set to "openai".
        if self.openai_api_key and self.llm_provider == LLMProviderName.OLLAMA:
            self.llm_provider = LLMProviderName.OPENAI

        if self.llm_provider == LLMProviderName.OPENAI and not self.openai_api_key:
            raise ConfigError(
                "LLM_PROVIDER=openai but OPENAI_API_KEY is blank. "
                "Either set OPENAI_API_KEY or switch LLM_PROVIDER=ollama."
            )

        if self.store_platform != StorePlatform.MOCK and not self.store_url:
            raise ConfigError(
                f"STORE_PLATFORM={self.store_platform.value} requires STORE_URL to be set."
            )

        if bool(self.gmc_merchant_id) != bool(self.gmc_service_account_json_path):
            raise ConfigError(
                "GMC_MERCHANT_ID and GMC_SERVICE_ACCOUNT_JSON_PATH must both be set, "
                "or both left blank. Partial GMC config is not allowed."
            )

        if self.llm_provider == LLMProviderName.OLLAMA and not self.ollama_host.startswith(("http://", "https://")):
            raise ConfigError(
                f"OLLAMA_HOST={self.ollama_host!r} is not a valid URL - it must start with "
                "http:// or https://. Fix this before startup rather than failing mid-run."
            )

        return self

    # --- derived mode flags -------------------------------------------------

    @property
    def store_is_live(self) -> bool:
        return self.store_platform != StorePlatform.MOCK

    @property
    def gmc_is_configured(self) -> bool:
        return bool(self.gmc_merchant_id and self.gmc_service_account_json_path)

    @property
    def notifications_are_live(self) -> bool:
        return bool(self.resend_api_key and self.alert_email_to)

    @property
    def api_auth_enabled(self) -> bool:
        return bool(self.api_auth_token)

    def describe_mode(self) -> str:
        lines = [
            "=" * 72,
            "GMC Compliance Bot - startup mode",
            "-" * 72,
            f"  Store:         {self.store_platform.value.upper()}"
            + ("" if self.store_is_live else "  (seeded mock catalog)"),
            f"  LLM provider:  {self.llm_provider.value.upper()}"
            + ("" if self.llm_provider == LLMProviderName.OPENAI else "  (local, no API cost)"),
            f"  GMC connect:   {'ENABLED' if self.gmc_is_configured else 'DISABLED - GMC not configured, skipping auto-connect'}",
            f"  Notifications: {'RESEND (live)' if self.notifications_are_live else 'LOGGED ONLY (no RESEND_API_KEY/ALERT_EMAIL_TO)'}",
            f"  API auth:      {'ENABLED (bearer token required)' if self.api_auth_enabled else 'DISABLED - /scan and /dashboard/alerts are open to anyone who can reach this server'}",
            "=" * 72,
        ]
        return "\n".join(lines)


def load_settings() -> Settings:
    try:
        settings = Settings()
    except ConfigError as exc:
        logger.error("Startup config error: %s", exc)
        print(f"FATAL: {exc}", file=sys.stderr)
        raise
    logger.info("\n%s", settings.describe_mode())
    print(settings.describe_mode())

    if not settings.api_auth_enabled and (settings.store_is_live or settings.gmc_is_configured):
        logger.warning(
            "Live store and/or GMC credentials are configured but API_AUTH_TOKEN is not set - "
            "anyone who can reach this server can read real compliance data and trigger a scan "
            "(and, once GMC is configured, a live claimwebsite call) via /scan and /dashboard/alerts. "
            "Set API_AUTH_TOKEN before exposing this beyond localhost."
        )

    return settings
