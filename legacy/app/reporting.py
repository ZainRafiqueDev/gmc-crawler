"""Severity-tagged issue reporting - the branch that fires whenever a scan
finds any violation. All-clear scans skip this module entirely; the pipeline
routes them straight to the GMC auto-connect step instead."""
from __future__ import annotations

import logging

from app.models.report import ComplianceReport
from app.notifications.base import EmailNotifier
from app.notifications.dashboard import DashboardAlertStore
from app.notifications.dedup import AlertDeduper

logger = logging.getLogger("gmc_compliance.reporting")


class IssueReportService:
    def __init__(self, notifier: EmailNotifier, dashboard_store: DashboardAlertStore, deduper: AlertDeduper) -> None:
        self._notifier = notifier
        self._dashboard_store = dashboard_store
        self._deduper = deduper

    async def handle_report(self, report: ComplianceReport) -> bool:
        """Returns True if the violations-found branch was taken."""
        if not report.all_violations:
            return False

        new_keys = self._deduper.new_keys(report.unresolved_violation_keys)
        if not new_keys:
            logger.info(
                "Scan %s: all %d violation(s) already alerted on previously - skipping duplicate notification",
                report.scan_id, len(report.all_violations),
            )
            return True

        await self._notifier.send_alert(report)
        self._dashboard_store.record(report)
        self._deduper.mark_alerted(new_keys)
        logger.info(
            "Scan %s: violations found (%d critical, %d warning) - alert sent, dashboard record written",
            report.scan_id, report.critical_count, report.warning_count,
        )
        return True
