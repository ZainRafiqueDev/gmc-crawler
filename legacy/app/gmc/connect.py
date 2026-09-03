"""The auto-connect gate.

Non-negotiable rule from the build brief: zero *critical* violations
required before `claimwebsite` is ever called. Warnings never block.
Account suspension is far more costly than a delayed connection, so this
gate fails closed - any doubt (report not clean, injection unconfirmed)
means we do not connect.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel

from app.gmc.client import GMCClient
from app.gmc.site_verification import SiteVerificationInjector, verification_meta_tag
from app.models.product import Product
from app.models.report import ComplianceReport

logger = logging.getLogger("gmc_compliance.gmc.connect")

MAX_VERIFICATION_ATTEMPTS = 3


class ConnectResult(BaseModel):
    connected: bool
    reason: str


async def inject_with_verification(
    verifier: SiteVerificationInjector, token: str, max_attempts: int = MAX_VERIFICATION_ATTEMPTS,
) -> bool:
    """Injects the verification tag and confirms it's actually live on the
    page before reporting success. Retries on failure; never proceeds
    silently."""
    for attempt in range(1, max_attempts + 1):
        ok = await verifier.inject(token)
        if ok:
            html = await verifier.fetch_page_html()
            if verification_meta_tag(token) in html or token in html:
                return True
            logger.warning(
                "Site verification injection call succeeded but tag not found on page "
                "(attempt %d/%d) - retrying.", attempt, max_attempts,
            )
        else:
            logger.warning("Site verification injection call failed (attempt %d/%d) - retrying.", attempt, max_attempts)
    logger.error("Site verification injection failed after %d attempts - aborting auto-connect.", max_attempts)
    return False


class AutoConnectGate:
    def __init__(self, gmc_client: GMCClient, verifier: SiteVerificationInjector) -> None:
        self._gmc_client = gmc_client
        self._verifier = verifier

    async def maybe_connect(self, report: ComplianceReport, products: list[Product]) -> ConnectResult:
        if not report.is_clean:
            reason = (
                f"Gate blocked: {report.critical_count} critical violation(s) present. "
                "claimwebsite will not be called."
            )
            logger.warning(reason)
            return ConnectResult(connected=False, reason=reason)

        token = await self._gmc_client.get_site_verification_token()
        verified = await inject_with_verification(self._verifier, token)
        if not verified:
            reason = "Site verification could not be confirmed - claimwebsite will not be called."
            logger.error(reason)
            return ConnectResult(connected=False, reason=reason)

        await self._gmc_client.claimwebsite()
        await self._gmc_client.submit_feed(products)
        reason = "Catalog clean, site verified, claimwebsite + feed submit succeeded."
        logger.info(reason)
        return ConnectResult(connected=True, reason=reason)
