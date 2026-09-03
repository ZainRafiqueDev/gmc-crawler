from __future__ import annotations

import logging

import httpx

from app.models.report import ComplianceReport
from app.notifications.base import EmailNotifier

logger = logging.getLogger("gmc_compliance.notifications.email")

RESEND_API_URL = "https://api.resend.com/emails"
FROM_ADDRESS = "gmc-compliance@alerts.local"


def _render_body(report: ComplianceReport) -> str:
    lines = [
        f"Compliance scan {report.scan_id}: "
        f"{report.critical_count} critical, {report.warning_count} warning violation(s).",
        "",
    ]
    for result in report.product_results:
        if not result.violations:
            continue
        lines.append(f"Product {result.product_id}:")
        for v in result.violations:
            lines.append(f"  [{v.severity.value.upper()}] {v.rule}: {v.message}")
    return "\n".join(lines)


class ResendEmailNotifier(EmailNotifier):
    def __init__(self, api_key: str, to_email: str, timeout_s: float = 15.0) -> None:
        self._api_key = api_key
        self._to_email = to_email
        self._timeout_s = timeout_s

    async def send_alert(self, report: ComplianceReport) -> None:
        payload = {
            "from": FROM_ADDRESS,
            "to": [self._to_email],
            "subject": f"GMC compliance: {report.critical_count} critical / {report.warning_count} warning",
            "text": _render_body(report),
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.post(RESEND_API_URL, json=payload, headers=headers)
            resp.raise_for_status()
        logger.info("Sent compliance alert email for scan %s", report.scan_id)


class LoggingEmailNotifier(EmailNotifier):
    """Fallback when RESEND_API_KEY/ALERT_EMAIL_TO aren't configured - never
    silently drops an alert, just logs it instead of emailing."""

    async def send_alert(self, report: ComplianceReport) -> None:
        logger.warning(
            "ALERT_EMAIL_TO/RESEND_API_KEY not configured - logging alert instead of emailing "
            "(scan %s: %d critical, %d warning)",
            report.scan_id, report.critical_count, report.warning_count,
        )
