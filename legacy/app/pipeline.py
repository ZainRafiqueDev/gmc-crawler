"""Full pipeline orchestration: collect -> compliance engine -> severity
branch -> (if GMC configured) auto-connect gate.

This is the single entry point both the scheduler's cron job and the
policy watcher's immediate-recheck trigger call into, so "daily run" and
"run right now because policy changed" are the exact same code path.
"""
from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel, ConfigDict

from app.collector import DataCollectorAgent
from app.config import Settings
from app.connectors.factory import get_store_connector
from app.engine import ComplianceEngine
from app.gmc.connect import AutoConnectGate, ConnectResult
from app.gmc.factory import get_gmc_client, get_site_verifier
from app.llm.factory import get_llm_provider
from app.models.report import ComplianceReport
from app.notifications.dashboard import DashboardAlertStore, InMemoryDashboardAlertStore
from app.notifications.dedup import InMemoryAlertDeduper
from app.notifications.factory import get_email_notifier
from app.reporting import IssueReportService

logger = logging.getLogger("gmc_compliance.pipeline")


class PipelineRunResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    report: ComplianceReport | None = None
    connect_result: ConnectResult | None = None
    skipped: bool = False


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        collector: DataCollectorAgent,
        engine: ComplianceEngine,
        issue_report_service: IssueReportService,
        gate: AutoConnectGate | None,
        dashboard_store: DashboardAlertStore,
    ) -> None:
        self.settings = settings
        self.collector = collector
        self.engine = engine
        self.issue_report_service = issue_report_service
        self.gate = gate
        self.dashboard_store = dashboard_store
        self._lock = asyncio.Lock()

    async def run_once(self) -> PipelineRunResult:
        if self._lock.locked():
            logger.warning("A pipeline run is already in progress - skipping this trigger to avoid a duplicate run.")
            return PipelineRunResult(skipped=True)

        async with self._lock:
            try:
                products = await self.collector.collect()
            except Exception as exc:  # noqa: BLE001 - store failure must not crash the process
                logger.error("Store fetch failed, aborting this run: %s", exc)
                return PipelineRunResult(skipped=True)

            report = await self.engine.run(products)
            await self.issue_report_service.handle_report(report)

            connect_result: ConnectResult | None = None
            if self.gate is not None:
                connect_result = await self.gate.maybe_connect(report, products)
            else:
                logger.info("GMC not configured - skipping auto-connect.")

            return PipelineRunResult(report=report, connect_result=connect_result)


def build_pipeline(settings: Settings) -> Pipeline:
    connector = get_store_connector(settings)
    collector = DataCollectorAgent(connector)

    llm_provider = get_llm_provider(settings)
    engine = ComplianceEngine(llm_provider)

    notifier = get_email_notifier(settings)
    dashboard_store = InMemoryDashboardAlertStore()
    deduper = InMemoryAlertDeduper()
    issue_report_service = IssueReportService(notifier, dashboard_store, deduper)

    gate: AutoConnectGate | None = None
    if settings.gmc_is_configured:
        gmc_client = get_gmc_client(settings)
        verifier = get_site_verifier(settings)
        gate = AutoConnectGate(gmc_client, verifier)

    return Pipeline(settings, collector, engine, issue_report_service, gate, dashboard_store)
