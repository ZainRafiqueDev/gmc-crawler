"""Google Merchant Center (Content API for Shopping) client.

`MockGMCClient` is the default everywhere except a fully-configured live
run - it records every call so tests (especially the auto-connect gate
test) can assert exactly which methods fired and in what order, without
needing real Google credentials.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

import httpx

from app.models.product import Product

logger = logging.getLogger("gmc_compliance.gmc.client")


class GMCClientError(RuntimeError):
    pass


class GMCClient(ABC):
    @abstractmethod
    async def get_site_verification_token(self) -> str:
        """Return the meta-tag token to inject into the store's <head>."""
        raise NotImplementedError

    @abstractmethod
    async def claimwebsite(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def submit_feed(self, products: list[Product]) -> None:
        raise NotImplementedError


class MockGMCClient(GMCClient):
    def __init__(self, token: str = "gmc-verify-token-mock") -> None:
        self._token = token
        self.call_log: list[str] = []

    async def get_site_verification_token(self) -> str:
        self.call_log.append("get_site_verification_token")
        return self._token

    async def claimwebsite(self) -> None:
        self.call_log.append("claimwebsite")

    async def submit_feed(self, products: list[Product]) -> None:
        self.call_log.append("submit_feed")

    @property
    def claimwebsite_called(self) -> bool:
        return "claimwebsite" in self.call_log


class ContentAPIGMCClient(GMCClient):
    """Real client, backed by the Content API for Shopping (v2.1) over
    plain httpx so it stays testable with respx like everything else.

    `access_token_provider` is a callable returning a fresh OAuth2 bearer
    token - kept pluggable so the service-account exchange (google-auth)
    can be swapped in without touching this class.
    """

    BASE_URL = "https://www.googleapis.com/content/v2.1"
    SITE_VERIFICATION_URL = "https://www.googleapis.com/siteVerification/v1/token"

    def __init__(
        self, merchant_id: str, access_token_provider: Callable[[], Awaitable[str]], timeout_s: float = 30.0,
    ) -> None:
        self._merchant_id = merchant_id
        self._access_token_provider = access_token_provider
        self._timeout_s = timeout_s

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._access_token_provider()}"}

    async def get_site_verification_token(self) -> str:
        payload = {"site": {"type": "SITE", "identifier": self._merchant_id}, "verificationMethod": "META"}
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.post(self.SITE_VERIFICATION_URL, json=payload, headers=await self._headers())
            resp.raise_for_status()
        data = resp.json()
        try:
            return data["token"]
        except KeyError as exc:
            raise GMCClientError(f"Site verification token response missing 'token': {data}") from exc

    async def claimwebsite(self) -> None:
        url = f"{self.BASE_URL}/{self._merchant_id}/accounts/{self._merchant_id}/claimwebsite"
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.post(url, headers=await self._headers())
            resp.raise_for_status()
        logger.info("GMC claimwebsite succeeded for merchant %s", self._merchant_id)

    async def submit_feed(self, products: list[Product]) -> None:
        url = f"{self.BASE_URL}/{self._merchant_id}/products/batch"
        entries = [{"batchId": i, "merchantId": self._merchant_id, "method": "insert",
                    "product": _product_to_feed_entry(p)} for i, p in enumerate(products)]
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.post(url, json={"entries": entries}, headers=await self._headers())
            resp.raise_for_status()
        logger.info("Submitted %d products to GMC feed for merchant %s", len(products), self._merchant_id)


def _product_to_feed_entry(product: Product) -> dict:
    return {
        "offerId": product.source_id,
        "title": product.title,
        "description": product.description,
        "price": {"value": str(product.price), "currency": product.currency},
        "gtin": product.gtin,
        "mpn": product.mpn,
        "condition": product.condition.value,
        "availability": product.availability,
        "imageLink": product.images[0].url if product.images else None,
    }
