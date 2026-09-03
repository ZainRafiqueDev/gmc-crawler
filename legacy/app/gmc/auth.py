"""Service-account OAuth2 token exchange for the Content API.

Real Google credentials aren't available yet, so this is a working
skeleton: drop a valid service-account JSON at
GMC_SERVICE_ACCOUNT_JSON_PATH and it performs the standard JWT-bearer
grant against Google's token endpoint. Every call is plain httpx, so it's
mockable with respx in tests exactly like the rest of the GMC client.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import jwt

TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/content"
_EXPIRY_SKEW_S = 60


class GoogleServiceAccountTokenProvider:
    def __init__(self, service_account_json_path: str, timeout_s: float = 15.0) -> None:
        raw = Path(service_account_json_path).read_text()
        self._creds = json.loads(raw)
        self._timeout_s = timeout_s
        self._cached_token: str | None = None
        self._cached_expiry: float = 0.0

    def _build_assertion(self) -> str:
        now = int(time.time())
        claims = {
            "iss": self._creds["client_email"],
            "scope": SCOPE,
            "aud": self._creds.get("token_uri", TOKEN_URI),
            "iat": now,
            "exp": now + 3600,
        }
        return jwt.encode(claims, self._creds["private_key"], algorithm="RS256")

    async def get_token(self) -> str:
        if self._cached_token and time.time() < self._cached_expiry - _EXPIRY_SKEW_S:
            return self._cached_token

        assertion = self._build_assertion()
        payload = {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.post(self._creds.get("token_uri", TOKEN_URI), data=payload)
            resp.raise_for_status()
        data = resp.json()
        self._cached_token = data["access_token"]
        self._cached_expiry = time.time() + float(data.get("expires_in", 3600))
        return self._cached_token
