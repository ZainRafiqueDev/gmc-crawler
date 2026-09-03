"""Dashboard alert records. In-memory by default; swap for a Postgres-backed
implementation (same interface) once the dashboard/API layer needs
persistence across restarts."""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.report import ComplianceReport


class DashboardAlertStore(ABC):
    @abstractmethod
    def record(self, report: ComplianceReport) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_alerts(self) -> list[ComplianceReport]:
        raise NotImplementedError


class InMemoryDashboardAlertStore(DashboardAlertStore):
    def __init__(self) -> None:
        self._alerts: list[ComplianceReport] = []

    def record(self, report: ComplianceReport) -> None:
        self._alerts.append(report)

    def list_alerts(self) -> list[ComplianceReport]:
        return list(self._alerts)
