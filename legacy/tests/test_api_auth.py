"""Proves /scan and /dashboard/alerts don't leak compliance data (or let
anyone trigger a scan) to an unauthenticated caller once API_AUTH_TOKEN is
configured, while /health stays open for uptime checks."""
from __future__ import annotations

import json

import httpx
import respx
from starlette.testclient import TestClient

from app.main import app

OLLAMA_URL = "http://localhost:11434/api/generate"


def _client(monkeypatch, **env) -> TestClient:
    for key in ("STORE_PLATFORM", "API_AUTH_TOKEN", "LLM_PROVIDER", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return TestClient(app)


@respx.mock
def test_scan_and_alerts_require_token_once_configured(monkeypatch):
    respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json={"response": json.dumps({"violations": []})}))
    with _client(monkeypatch, API_AUTH_TOKEN="s3cr3t") as client:
        assert client.get("/health").status_code == 200  # public, no token needed

        no_auth = client.post("/scan")
        assert no_auth.status_code == 401

        wrong_auth = client.get("/dashboard/alerts", headers={"Authorization": "Bearer wrong"})
        assert wrong_auth.status_code == 401

        ok = client.post("/scan", headers={"Authorization": "Bearer s3cr3t"})
        assert ok.status_code == 200


@respx.mock
def test_scan_accessible_without_token_when_auth_not_configured_dev_mode(monkeypatch):
    respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json={"response": json.dumps({"violations": []})}))
    with _client(monkeypatch) as client:  # no API_AUTH_TOKEN set -> mock-mode dev convenience
        assert client.post("/scan").status_code == 200


def test_health_response_contains_no_secrets(monkeypatch):
    with _client(monkeypatch, STORE_API_KEY="should-never-appear", OPENAI_API_KEY="sk-should-never-appear") as client:
        body = client.get("/health").text
        assert "should-never-appear" not in body
        assert "sk-should-never-appear" not in body
