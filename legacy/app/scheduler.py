"""Cron scheduling: daily full-catalog recheck, plus an immediate trigger
whenever the GMC Policy Watcher detects a change. Both paths call the same
`Pipeline.run_once`, which already has its own concurrency guard - so
whichever trigger loses a race just logs a skip instead of double-running.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.pipeline import Pipeline, PipelineRunResult
from app.policy_watcher.watcher import PolicyCheckResult, PolicyWatcher

logger = logging.getLogger("gmc_compliance.scheduler")

DAILY_JOB_ID = "daily-compliance-recheck"


async def run_scheduled_scan(pipeline: Pipeline) -> PipelineRunResult:
    logger.info("Scheduled compliance recheck starting.")
    return await pipeline.run_once()


def make_policy_change_handler(pipeline: Pipeline):
    async def _on_policy_change(result: PolicyCheckResult) -> None:
        logger.warning(
            "GMC policy change detected (%s) - triggering immediate full recheck.", result.url,
        )
        await pipeline.run_once()

    return _on_policy_change


class ComplianceScheduler:
    def __init__(self, pipeline: Pipeline, policy_watcher: PolicyWatcher | None = None, policy_urls: list[str] | None = None) -> None:
        self._pipeline = pipeline
        self._policy_watcher = policy_watcher
        self._policy_urls = policy_urls or []
        self._scheduler = AsyncIOScheduler()

    def start(self, hour: int = 3, minute: int = 0) -> None:
        self._scheduler.add_job(
            run_scheduled_scan, CronTrigger(hour=hour, minute=minute),
            args=[self._pipeline], id=DAILY_JOB_ID, replace_existing=True,
        )
        if self._policy_watcher is not None and self._policy_urls:
            self._scheduler.add_job(
                self._run_policy_checks, CronTrigger(hour=hour, minute=minute - 15 if minute >= 15 else 45),
                id="daily-policy-watch", replace_existing=True,
            )
        self._scheduler.start()
        logger.info("Scheduler started: daily recheck at %02d:%02d.", hour, minute)

    async def _run_policy_checks(self) -> None:
        for url in self._policy_urls:
            await self._policy_watcher.check(url)

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)
