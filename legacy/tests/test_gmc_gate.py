"""The most important test file in the suite: proves the auto-connect gate
fails closed on any critical violation, regardless of GMC config presence."""
from __future__ import annotations

from app.gmc.client import MockGMCClient
from app.gmc.connect import AutoConnectGate, inject_with_verification
from app.gmc.site_verification import MockSiteVerificationInjector, verification_meta_tag
from app.models.product import Product, ProductCategory, ProductImage
from app.models.report import CheckSource, ComplianceReport, ProductResult, Severity, Violation

GOOD_IMAGE = [ProductImage(url="https://x/img.jpg", width_px=1200, height_px=1200)]


def _product(pid: str) -> Product:
    return Product(
        id=pid, source_id=pid, title="T", description="D", price=10.0, landing_page_price=10.0,
        images=GOOD_IMAGE, gtin="00012345678905", category=ProductCategory.HOUSEHOLD_TOOL,
    )


def _report_with(violations: list[Violation]) -> ComplianceReport:
    result = ProductResult(product_id="p1", violations=violations)
    return ComplianceReport(scan_id="scan-1", product_results=[result])


async def test_one_critical_violation_never_triggers_claimwebsite_even_with_full_gmc_config():
    report = _report_with([
        Violation(product_id="p1", rule="gtin_present", severity=Severity.CRITICAL,
                   source=CheckSource.DETERMINISTIC, message="missing gtin"),
    ])
    gmc_client = MockGMCClient()
    verifier = MockSiteVerificationInjector()
    gate = AutoConnectGate(gmc_client, verifier)

    result = await gate.maybe_connect(report, [_product("p1")])

    assert result.connected is False
    assert gmc_client.claimwebsite_called is False
    assert "claimwebsite" not in gmc_client.call_log


async def test_warnings_only_catalog_does_proceed_to_connection():
    report = _report_with([
        Violation(product_id="p1", rule="image_quality", severity=Severity.WARNING,
                   source=CheckSource.DETERMINISTIC, message="image below recommended size"),
    ])
    gmc_client = MockGMCClient()
    verifier = MockSiteVerificationInjector()
    gate = AutoConnectGate(gmc_client, verifier)

    result = await gate.maybe_connect(report, [_product("p1")])

    assert result.connected is True
    assert gmc_client.call_log == ["get_site_verification_token", "claimwebsite", "submit_feed"]


async def test_clean_catalog_call_sequence_is_verify_then_claim_then_submit():
    report = _report_with([])
    gmc_client = MockGMCClient()
    verifier = MockSiteVerificationInjector()
    gate = AutoConnectGate(gmc_client, verifier)

    result = await gate.maybe_connect(report, [_product("p1")])

    assert result.connected is True
    assert gmc_client.call_log == ["get_site_verification_token", "claimwebsite", "submit_feed"]


async def test_site_verification_tag_appears_in_fetched_html_after_injection():
    verifier = MockSiteVerificationInjector()
    ok = await inject_with_verification(verifier, "tok-123")
    assert ok is True
    html = await verifier.fetch_page_html()
    assert verification_meta_tag("tok-123") in html


async def test_failed_injection_is_retried_then_logged_not_silently_proceeding():
    verifier = MockSiteVerificationInjector(fail_times=5)  # exceeds MAX_VERIFICATION_ATTEMPTS
    gmc_client = MockGMCClient()
    gate = AutoConnectGate(gmc_client, verifier)
    report = _report_with([])

    result = await gate.maybe_connect(report, [_product("p1")])

    assert result.connected is False
    assert "claimwebsite" not in gmc_client.call_log


async def test_injection_succeeds_after_retrying_transient_failures():
    verifier = MockSiteVerificationInjector(fail_times=2)  # fails twice, succeeds on 3rd (within max attempts)
    ok = await inject_with_verification(verifier, "tok-456")
    assert ok is True
