from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings

_ALL_ENV_KEYS = [
    "STORE_PLATFORM", "STORE_URL", "STORE_API_KEY", "STORE_API_SECRET",
    "LLM_PROVIDER", "OPENAI_API_KEY", "OLLAMA_HOST", "OLLAMA_MODEL",
    "GMC_MERCHANT_ID", "GMC_SERVICE_ACCOUNT_JSON_PATH",
    "ALERT_EMAIL_TO", "RESEND_API_KEY", "API_AUTH_TOKEN", "DATABASE_URL",
]


@pytest.fixture
def make_settings(monkeypatch):
    """Builds a Settings instance from exactly the given overrides - clears
    every relevant env var first so tests never inherit host-machine state
    or a stray local .env file."""

    def _make(**overrides: str) -> Settings:
        for key in _ALL_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        for key, value in overrides.items():
            monkeypatch.setenv(key.upper(), value)
        return Settings(_env_file=None)

    return _make


@pytest.fixture(scope="session")
def fake_service_account_path(tmp_path_factory) -> str:
    """A throwaway service-account JSON with a freshly generated RSA key,
    written to a per-session tmp dir rather than committed to the repo -
    nothing that looks like a real credential should ever sit in git
    history, even a fake one that a secret scanner can't tell apart from
    a real leak."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    data = {
        "type": "service_account",
        "project_id": "gmc-compliance-test",
        "private_key_id": "test-key-id",
        "private_key": pem,
        "client_email": "gmc-compliance-test@gmc-compliance-test.iam.gserviceaccount.com",
        "client_id": "000000000000000000000",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    path = tmp_path_factory.mktemp("gmc-fixtures") / "fake_service_account.json"
    path.write_text(json.dumps(data))
    return str(path)
