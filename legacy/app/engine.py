"""AI Compliance Engine.

Runs the deterministic track (always) and the LLM track (best-effort) for
every product and assembles a `ComplianceReport`. The two tracks are kept
strictly separate at the call level - deterministic checks never touch the
network, and an LLM failure for one product never aborts the run for the
rest of the catalog.
"""
from __future__ import annotations

import logging
import uuid

from app.llm.base import LLMProvider, LLMProviderError
from app.llm.checks import run_llm_checks
from app.models.product import Product
from app.models.report import ComplianceReport, ProductResult
from app.rules.deterministic import run_all_deterministic_checks

logger = logging.getLogger("gmc_compliance.engine")


class ComplianceEngine:
    def __init__(self, llm_provider: LLMProvider) -> None:
        self._llm_provider = llm_provider

    async def _evaluate_product(self, product: Product) -> ProductResult:
        result = ProductResult(product_id=product.id)
        result.violations.extend(run_all_deterministic_checks(product))

        try:
            result.violations.extend(await run_llm_checks(product, self._llm_provider))
        except LLMProviderError as exc:
            logger.warning("LLM check failed for product %s, flagging for manual review: %s", product.id, exc)
            result.needs_manual_review = True
            result.review_reason = str(exc)
        except Exception as exc:  # noqa: BLE001 - isolate one product's failure from the whole scan
            logger.error("Unexpected error running LLM check for product %s: %s", product.id, exc)
            result.needs_manual_review = True
            result.review_reason = f"Unexpected error: {exc}"

        return result

    async def run(self, products: list[Product]) -> ComplianceReport:
        scan_id = str(uuid.uuid4())
        results = [await self._evaluate_product(p) for p in products]
        report = ComplianceReport(scan_id=scan_id, product_results=results)
        logger.info(
            "Scan %s complete: %d products, %d critical, %d warning",
            scan_id, len(products), report.critical_count, report.warning_count,
        )
        return report
