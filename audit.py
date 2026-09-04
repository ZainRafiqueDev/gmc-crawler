#!/usr/bin/env python
"""Step 7: CLI entry point.

    python audit.py --url https://example.com [--wc-key ... --wc-secret ...]

Runs the full LangGraph audit pipeline (detect platform -> crawl -> classify
-> deterministic checks -> LLM-graded checks -> report) and writes the
Markdown report to disk.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

from app.checks.purchase_journey import run_purchase_journey_check
from app.config import load_settings
from app.db import Database
from app.graph import run_audit
from app.llm.cache import LLMCache
from app.models import PageType
from app.report import generate_markdown_report, safe_host_for_filename
from app.security.ssrf_guard import SSRFBlockedError, assert_public_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("gmc_audit.cli")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GMC compliance audit for a WordPress/WooCommerce store.")
    parser.add_argument("--url", required=True, help="Store URL to audit, e.g. https://example.com")
    parser.add_argument("--wc-key", default=None, help="WooCommerce REST API consumer key (overrides WC_CONSUMER_KEY env var)")
    parser.add_argument("--wc-secret", default=None, help="WooCommerce REST API consumer secret (overrides WC_CONSUMER_SECRET env var)")
    parser.add_argument("--max-pages", type=int, default=None, help="Override CRAWL_MAX_PAGES for this run")
    parser.add_argument("--max-depth", type=int, default=None, help="Override CRAWL_MAX_DEPTH for this run")
    parser.add_argument("--output-dir", default=None, help="Override REPORT_OUTPUT_DIR for this run")
    parser.add_argument("--no-cache", action="store_true", help="Disable the LLM/vision result cache for this run (always call the API fresh).")

    journey_group = parser.add_argument_group(
        "purchase-journey verification (Phase E - opt-in, off by default)",
        "Adds a sample product to cart and reads checkout totals. NEVER submits payment or clicks any "
        "place-order/pay control - see app/checks/purchase_journey.py for the safety design. "
        "Both flags below are required together; running this against a store NOT in a sandbox/test "
        "payment mode risks real side effects (real cart/session state, potentially real fraud-detection "
        "signals) even though no payment is ever submitted.",
    )
    journey_group.add_argument("--enable-purchase-journey", action="store_true", help="Opt in to the purchase-journey check for this run.")
    journey_group.add_argument(
        "--confirm-test-payment-mode", action="store_true",
        help="Required alongside --enable-purchase-journey: confirms you have verified this store is in a sandbox/test payment mode.",
    )
    journey_group.add_argument("--purchase-journey-product-url", default=None, help="Specific product URL to use; defaults to the first crawled product page.")

    return parser.parse_args(argv)


def _report_filename(url: str) -> str:
    host = safe_host_for_filename(url)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{host}-{timestamp}.md"


def _format_action_log_markdown(result) -> str:
    lines = [
        "## Purchase Journey Action Log (Phase E)",
        "",
        f"Stopped before payment: **{result.stopped_before_payment}** (structural - this check never clicks a payment/place-order control)",
        "",
    ]
    for entry in result.action_log:
        detail = f" - {entry.detail}" if entry.detail else ""
        lines.append(f"- `{entry.timestamp}` **{entry.action}**{detail}")
    return "\n".join(lines) + "\n"


async def main_async(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings()

    if args.enable_purchase_journey and not args.confirm_test_payment_mode:
        print(
            "Error: --enable-purchase-journey requires --confirm-test-payment-mode.\n"
            "This confirms you have verified the target store is running a sandbox/test payment "
            "gateway (e.g. WooCommerce Test Mode, Shopify's Bogus Gateway) before this runs an "
            "add-to-cart/checkout flow against it. See app/checks/purchase_journey.py for exactly "
            "what this does and does not do."
        )
        return 1

    if args.wc_key:
        settings.wc_consumer_key = args.wc_key
    if args.wc_secret:
        settings.wc_consumer_secret = args.wc_secret
    if args.max_pages:
        settings.crawl_max_pages = args.max_pages
        settings.crawl_max_pages_explicit = True
    if args.max_depth:
        settings.crawl_max_depth = args.max_depth
    if args.output_dir:
        settings.report_output_dir = Path(args.output_dir)

    if not settings.llm_configured:
        logger.warning("ANTHROPIC_API_KEY not set - LLM-graded checks (step 5) will be skipped and reported as CANNOT VERIFY.")

    normalized_url = args.url if "://" in args.url else f"https://{args.url}"
    try:
        await assert_public_url(normalized_url)
    except SSRFBlockedError as exc:
        print(f"Error: refusing to audit {args.url!r} - {exc}")
        return 1

    journey_markdown: str | None = None

    # --no-cache means "don't trust cached LLM/vision grading results" - it
    # does not mean "no database at all". The real RAG policy index
    # (app/llm/policy_rag.py) needs DB access regardless of that flag; a
    # database connection is always created, only llm_cache is conditional
    # on it. (Verified live: deriving RAG's DB access from llm_cache.db
    # instead of a real, independent db= parameter silently fell back to
    # the stub policy snippets under --no-cache - a real bug, not
    # hypothetical, fixed by keeping these two things separate here.)
    db = Database(settings.database_url)
    await db.init()
    llm_cache: LLMCache | None = None if args.no_cache else LLMCache(db)

    async with async_playwright() as pw:
        # See app/api/main.py's lifespan for why --disable-dev-shm-usage matters
        # in a memory-constrained container.
        browser = await pw.chromium.launch(args=["--disable-dev-shm-usage"])
        try:
            try:
                result = await asyncio.wait_for(
                    run_audit(args.url, settings, browser, llm_cache, db), timeout=settings.audit_timeout_seconds,
                )
            except asyncio.TimeoutError:
                print(f"Error: audit exceeded the {settings.audit_timeout_seconds}s time budget and was aborted.")
                return 1

            if args.enable_purchase_journey:
                product_url = args.purchase_journey_product_url
                if not product_url:
                    product_page = next((p for p in result["site_map"].pages_of_type(PageType.PRODUCT) if p.reachable), None)
                    product_url = product_page.url if product_page else None

                if not product_url:
                    logger.warning("--enable-purchase-journey set but no product page was found/specified - skipping.")
                else:
                    logger.warning(
                        "PURCHASE JOURNEY CHECK ENABLED - adding a real item to cart and loading checkout on %s. "
                        "This never submits payment, but confirm this store is in test/sandbox payment mode.",
                        product_url,
                    )
                    journey_result = await run_purchase_journey_check(browser, result["platform"].base_url, product_url)
                    result["findings"] = result.get("findings", []) + journey_result.findings
                    # Regenerate the report so journey findings appear in the severity
                    # sections/page-by-page/executive-summary like every other finding -
                    # they were produced after compile_report_node already ran.
                    cache_stats = (llm_cache.hits, llm_cache.misses) if llm_cache is not None else None
                    result["report_markdown"] = generate_markdown_report(
                        result["platform"], result["site_map"], result["findings"], cache_stats=cache_stats,
                        llm_coverage=result.get("llm_coverage"),
                    )
                    journey_markdown = _format_action_log_markdown(journey_result)
        finally:
            await browser.close()
            if db is not None:
                await db.dispose()

    settings.report_output_dir.mkdir(parents=True, exist_ok=True)
    output_path = settings.report_output_dir / _report_filename(args.url)
    report_text = result["report_markdown"]
    if journey_markdown:
        report_text = report_text + "\n\n" + journey_markdown
    output_path.write_text(report_text, encoding="utf-8")

    findings = result.get("findings", [])
    critical_count = sum(1 for f in findings if f.severity.value == "critical")
    logger.info("Audit complete: %d pages crawled, %d findings (%d critical). Report written to %s", len(result["site_map"].pages), len(findings), critical_count, output_path)
    print(f"\nReport written to: {output_path}")
    return 0


def main() -> None:
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
