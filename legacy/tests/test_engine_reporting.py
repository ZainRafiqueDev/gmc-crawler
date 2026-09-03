from __future__ import annotations

import json

from app.engine import ComplianceEngine
from app.llm.base import LLMProviderError
from app.llm.mock import MockLLMProvider
from app.models.product import Product, ProductCategory, ProductImage
from app.notifications.dashboard import InMemoryDashboardAlertStore
from app.notifications.dedup import InMemoryAlertDeduper
from app.reporting import IssueReportService

GOOD_IMAGE = [ProductImage(url="https://x/img.jpg", width_px=1200, height_px=1200)]


def _clean_tool() -> Product:
    return Product(
        id="tool-1", source_id="s1", title="Grabber", description="A grabber tool.",
        price=10.0, landing_page_price=10.0, images=GOOD_IMAGE, gtin="00012345678905",
        category=ProductCategory.HOUSEHOLD_TOOL,
    )


def _critical_tool() -> Product:
    return Product(
        id="tool-2", source_id="s2", title="Grabber 2", description="Another grabber.",
        price=10.0, landing_page_price=10.0, images=GOOD_IMAGE, gtin=None,  # missing GTIN -> critical
        category=ProductCategory.HOUSEHOLD_TOOL,
    )


def _warning_coffee() -> Product:
    return Product(
        id="coffee-1", source_id="s3", title="EspressoPro", description="Refurbished espresso machine.",
        price=90.0, landing_page_price=90.0, images=GOOD_IMAGE, gtin="00012345678912",
        category=ProductCategory.COFFEE_MACHINE, condition="refurbished",  # no warranty mention -> warning
    )


async def test_engine_produces_correct_severity_tags():
    llm = MockLLMProvider(lambda s, u: json.dumps({"violations": []}))
    engine = ComplianceEngine(llm)
    report = await engine.run([_clean_tool(), _critical_tool(), _warning_coffee()])

    assert report.critical_count == 1
    assert report.warning_count == 1
    assert report.is_clean is False


async def test_engine_llm_failure_flags_manual_review_without_crashing_run():
    def _raise(system: str, user: str) -> str:
        raise LLMProviderError("simulated timeout")

    llm = MockLLMProvider(_raise)
    engine = ComplianceEngine(llm)
    report = await engine.run([_clean_tool()])

    result = report.product_results[0]
    assert result.needs_manual_review is True
    assert result.review_reason is not None


class _RecordingNotifier:
    def __init__(self) -> None:
        self.sent = []

    async def send_alert(self, report) -> None:
        self.sent.append(report)


async def test_violations_branch_sends_alert_and_writes_dashboard_record():
    llm = MockLLMProvider(lambda s, u: json.dumps({"violations": []}))
    engine = ComplianceEngine(llm)
    report = await engine.run([_critical_tool(), _warning_coffee()])

    notifier = _RecordingNotifier()
    dashboard = InMemoryDashboardAlertStore()
    deduper = InMemoryAlertDeduper()
    service = IssueReportService(notifier, dashboard, deduper)

    branch_taken = await service.handle_report(report)

    assert branch_taken is True
    assert len(notifier.sent) == 1
    assert len(dashboard.list_alerts()) == 1


async def test_clean_report_does_not_trigger_alert_branch():
    llm = MockLLMProvider(lambda s, u: json.dumps({"violations": []}))
    engine = ComplianceEngine(llm)
    report = await engine.run([_clean_tool()])

    notifier = _RecordingNotifier()
    dashboard = InMemoryDashboardAlertStore()
    service = IssueReportService(notifier, dashboard, InMemoryAlertDeduper())

    branch_taken = await service.handle_report(report)

    assert branch_taken is False
    assert notifier.sent == []
    assert dashboard.list_alerts() == []


async def test_repeat_run_with_same_unresolved_violations_does_not_duplicate_alert():
    llm = MockLLMProvider(lambda s, u: json.dumps({"violations": []}))
    engine = ComplianceEngine(llm)
    notifier = _RecordingNotifier()
    dashboard = InMemoryDashboardAlertStore()
    service = IssueReportService(notifier, dashboard, InMemoryAlertDeduper())

    report1 = await engine.run([_critical_tool()])
    report2 = await engine.run([_critical_tool()])

    await service.handle_report(report1)
    await service.handle_report(report2)

    assert len(notifier.sent) == 1
    assert len(dashboard.list_alerts()) == 1
