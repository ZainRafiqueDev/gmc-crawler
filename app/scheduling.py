"""Scheduling backend interface (Goal 2.5). `MonitorService` only calls
through `SchedulerBackend` - never touches APScheduler directly - so
swapping to Celery/RQ later means writing one new class here, not touching
the monitoring/audit logic.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Protocol

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger("gmc_audit.scheduling")

JobFunc = Callable[..., Awaitable[None]]


class SchedulerBackend(Protocol):
    def add_interval_job(self, job_id: str, func: JobFunc, days: int, **kwargs) -> None: ...
    def remove_job(self, job_id: str) -> None: ...
    def has_job(self, job_id: str) -> bool: ...
    def start(self) -> None: ...
    def shutdown(self) -> None: ...


class APSchedulerBackend:
    """Single-process scheduler, good for the current scale (per the
    original project brief: APScheduler for now, note where this would move
    to Celery/RQ later - this class is that seam).
    """

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()

    def add_interval_job(self, job_id: str, func: JobFunc, days: int, **kwargs) -> None:
        self._scheduler.add_job(func, "interval", days=days, id=job_id, replace_existing=True, kwargs=kwargs)
        logger.info("Scheduled job %r every %d day(s)", job_id, days)

    def remove_job(self, job_id: str) -> None:
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)
            logger.info("Removed job %r", job_id)

    def has_job(self, job_id: str) -> bool:
        return self._scheduler.get_job(job_id) is not None

    def start(self) -> None:
        self._scheduler.start()

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)
