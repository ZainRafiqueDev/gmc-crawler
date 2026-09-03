from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.report import ComplianceReport


class EmailNotifier(ABC):
    @abstractmethod
    async def send_alert(self, report: ComplianceReport) -> None:
        raise NotImplementedError
