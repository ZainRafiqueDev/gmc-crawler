"""LangGraph orchestration of the audit pipeline: detect platform -> crawl ->
classify pages -> run deterministic checks -> grade with LLM -> compile
report. Each stage is one graph node so later phases (Shopify support,
snapshot diffing, scheduled re-runs) can insert/branch without restructuring
the whole pipeline.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable, TypedDict

from langgraph.graph import END, StateGraph
from playwright.async_api import Browser

from app.checks.business_identity import check_business_identity_consistency
from app.checks.deterministic import run_all_deterministic_checks
from app.checks.duplicate_products import check_duplicate_products
from app.checks.form_checks import check_forms
from app.checks.product_checks import run_product_checks
from app.checks.product_images import ProductImage
from app.checks.screenshot_annotator import capture_annotated_screenshots
from app.config import Settings
from app.db import Database
from app.ads_eligibility import apply_ads_eligibility_impact
from app.impact_tier import apply_impact_tiers
from app.llm.cache import LLMCache
from app.llm.checks import run_llm_checks
from app.llm.image_checks import run_llm_image_checks
from app.models import Finding, LLMCoverageStats, PlatformDetectionResult, SiteMap
from app.platform_detector import detect_platform
from app.proxy_config import build_proxy_rotator, to_httpx_proxy_url
from app.report import generate_markdown_report, safe_host_for_filename
from app.security.ssrf_guard import reset_current_proxy, set_current_proxy
from app.site_mapper import map_site

logger = logging.getLogger("gmc_audit.graph")


class AuditState(TypedDict, total=False):
    url: str
    settings: Settings
    browser: Browser
    llm_cache: LLMCache | None
    # Separate from llm_cache on purpose: the real RAG policy index
    # (app/llm/policy_rag.py) needs DB access independent of whether LLM
    # *result* caching happens to be enabled - audit.py's --no-cache flag
    # used to silently disable RAG retrieval too, as a side effect of
    # deriving DB access from cache.db (verified live, a real bug). db
    # defaults to llm_cache.db in run_llm_checks if not given explicitly,
    # for callers that only ever have the two bundled together anyway.
    db: Database | None
    platform: PlatformDetectionResult
    site_map: SiteMap
    findings: list[Finding]
    product_images: dict[str, list[ProductImage]]
    llm_coverage: LLMCoverageStats
    report_markdown: str


async def _detect_platform_node(state: AuditState) -> dict:
    result = await detect_platform(state["url"])
    logger.info("Platform detected: %s (%s)", result.platform.value, result.base_url)
    return {"platform": result}


async def _crawl_and_classify_node(state: AuditState) -> dict:
    site_map = await map_site(state["platform"].base_url, state["browser"], state["settings"], platform=state["platform"].platform)
    logger.info("Crawl complete: %d pages", len(site_map.pages))
    return {"site_map": site_map}


async def _deterministic_checks_node(state: AuditState) -> dict:
    site_map = state["site_map"]
    settings = state["settings"]

    findings = await run_all_deterministic_checks(site_map)
    findings.extend(check_business_identity_consistency(site_map))
    findings.extend(await check_forms(site_map))
    findings.extend(check_duplicate_products(site_map))
    product_findings, product_images = await run_product_checks(site_map, state["platform"], settings)
    findings.extend(product_findings)

    logger.info("Deterministic checks complete: %d findings", len(findings))
    return {"findings": findings, "product_images": product_images}


async def _llm_grading_node(state: AuditState) -> dict:
    cache = state.get("llm_cache")
    db = state.get("db")
    llm_findings, llm_coverage = await run_llm_checks(state["site_map"], state["settings"], cache, db)
    image_findings = await run_llm_image_checks(state["site_map"], state.get("product_images", {}), state["settings"], cache)
    if cache is not None:
        logger.info("LLM cache: %d hit(s), %d miss(es) this run", cache.hits, cache.misses)
    logger.info("LLM-graded checks complete: %d findings (%d image, %d text)", len(llm_findings) + len(image_findings), len(image_findings), len(llm_findings))
    logger.info(
        "Product-page LLM sample coverage: %d/%d (%s)", llm_coverage.product_pages_checked, llm_coverage.total_reachable_product_pages,
        "not configured" if not llm_coverage.llm_configured else f"{llm_coverage.coverage_fraction:.0%}" if llm_coverage.coverage_fraction is not None else "n/a",
    )
    all_findings = apply_ads_eligibility_impact(apply_impact_tiers(state.get("findings", []) + llm_findings + image_findings))
    return {"findings": all_findings, "llm_coverage": llm_coverage}


async def _compile_report_node(state: AuditState) -> dict:
    cache = state.get("llm_cache")
    cache_stats = (cache.hits, cache.misses) if cache is not None else None
    settings = state["settings"]

    findings = state["findings"]
    try:
        findings = await capture_annotated_screenshots(
            state["browser"], findings, settings, Path(settings.report_output_dir),
            filename_prefix=safe_host_for_filename(state["platform"].base_url),
        )
    except Exception:
        # Screenshot capture is a report-polish add-on, never load-bearing -
        # a failure here must degrade to "no screenshots this run", not fail
        # the whole audit.
        logger.exception("Annotated screenshot capture failed - continuing without screenshots for this run")

    report_markdown = generate_markdown_report(
        state["platform"], state["site_map"], findings, cache_stats=cache_stats, llm_coverage=state.get("llm_coverage"),
    )
    return {"report_markdown": report_markdown, "findings": findings}


def build_audit_graph():
    graph = StateGraph(AuditState)
    graph.add_node("detect_platform", _detect_platform_node)
    graph.add_node("crawl_and_classify", _crawl_and_classify_node)
    graph.add_node("deterministic_checks", _deterministic_checks_node)
    graph.add_node("llm_grading", _llm_grading_node)
    graph.add_node("compile_report", _compile_report_node)

    graph.set_entry_point("detect_platform")
    graph.add_edge("detect_platform", "crawl_and_classify")
    graph.add_edge("crawl_and_classify", "deterministic_checks")
    graph.add_edge("deterministic_checks", "llm_grading")
    graph.add_edge("llm_grading", "compile_report")
    graph.add_edge("compile_report", END)
    return graph.compile()


def _current_proxy_for(settings: Settings) -> str | None:
    """The single httpx proxy URL to use for this whole audit run's non-
    Playwright traffic (platform detection, sitemap fetch, image checks,
    WC/Shopify API calls) - opt-in, app.proxy_config, None when unconfigured."""
    rotator = build_proxy_rotator(settings)
    return to_httpx_proxy_url(rotator.next()) if rotator else None


async def run_audit(
    url: str, settings: Settings, browser: Browser, llm_cache: LLMCache | None = None, db: Database | None = None,
) -> AuditState:
    graph = build_audit_graph()
    # Set once for the whole audit (not just the crawl node) so platform
    # detection - which runs first - is consistent with everything after it.
    token = set_current_proxy(_current_proxy_for(settings))
    try:
        return await graph.ainvoke({"url": url, "settings": settings, "browser": browser, "llm_cache": llm_cache, "db": db})  # type: ignore[return-value]
    finally:
        reset_current_proxy(token)


# Human-readable phase names, in pipeline order - used by run_audit_streaming
# (the frontend's polling progress display) to translate a raw node name.
PHASE_LABELS: dict[str, str] = {
    "detect_platform": "Detecting platform",
    "crawl_and_classify": "Crawling and classifying pages",
    "deterministic_checks": "Running deterministic checks",
    "llm_grading": "Grading with LLM",
    "compile_report": "Compiling report",
}


async def run_audit_streaming(
    url: str, settings: Settings, browser: Browser, llm_cache: LLMCache | None,
    on_phase: Callable[[str], Awaitable[None]] | None = None, db: Database | None = None,
) -> AuditState:
    """Same pipeline as run_audit, but awaits on_phase(node_name) as each
    stage completes - lets a caller (the API's background job runner) report
    real phase-by-phase progress instead of a single opaque "running" state.
    on_phase is async so the caller can persist progress (e.g. to the DB)
    without a fire-and-forget task.
    """
    graph = build_audit_graph()
    final_state: dict = {}
    token = set_current_proxy(_current_proxy_for(settings))
    try:
        async for chunk in graph.astream({"url": url, "settings": settings, "browser": browser, "llm_cache": llm_cache, "db": db}):
            for node_name, node_output in chunk.items():
                final_state.update(node_output)
                if on_phase is not None:
                    await on_phase(node_name)
    finally:
        reset_current_proxy(token)
    return final_state  # type: ignore[return-value]
