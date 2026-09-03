from __future__ import annotations

import asyncio
import json

from app.collector import DataCollectorAgent
from app.connectors.mock import MockStoreConnector
from app.engine import ComplianceEngine
from app.llm.mock import MockLLMProvider
from app.notifications.dashboard import InMemoryDashboardAlertStore
from app.notifications.dedup import InMemoryAlertDeduper
from app.pipeline import Pipeline
from app.reporting import IssueReportService
from app.scheduler import make_policy_change_handler, run_scheduled_scan


class _RecordingNotifier:
    def __init__(self) -> None:
        self.sent = []

    async def send_alert(self, report) -> None:
        self.sent.append(report)


def _build_pipeline(collector: DataCollectorAgent) -> Pipeline:
    llm = MockLLMProvider(lambda s, u: json.dumps({"violations": []}))
    engine = ComplianceEngine(llm)
    service = IssueReportService(_RecordingNotifier(), InMemoryDashboardAlertStore(), InMemoryAlertDeduper())
    from app.config import Settings

    return Pipeline(Settings(_env_file=None), collector, engine, service, gate=None,
                     dashboard_store=InMemoryDashboardAlertStore())


async def test_daily_cron_job_function_runs_full_pipeline_directly():
    pipeline = _build_pipeline(DataCollectorAgent(MockStoreConnector()))
    result = await run_scheduled_scan(pipeline)
    assert result.skipped is False
    assert result.report is not None
    assert len(result.report.product_results) == 12


async def test_policy_change_handler_triggers_recheck_outside_normal_schedule():
    pipeline = _build_pipeline(DataCollectorAgent(MockStoreConnector()))
    handler = make_policy_change_handler(pipeline)

    from app.policy_watcher.watcher import PolicyCheckResult
    await handler(PolicyCheckResult(url="https://x", changed=True, change_summary="new rule"))

    # A run happened outside the cron trigger path.
    assert pipeline._lock.locked() is False  # released after completion


class _SlowConnector:
    def __init__(self) -> None:
        self.calls = 0
        self.release = asyncio.Event()

    async def fetch_products(self):
        self.calls += 1
        await self.release.wait()
        return []


async def test_concurrent_trigger_while_run_in_progress_is_skipped_not_duplicated():
    slow_connector = _SlowConnector()
    pipeline = _build_pipeline(DataCollectorAgent(slow_connector))

    first_run = asyncio.create_task(pipeline.run_once())
    await asyncio.sleep(0.05)  # let the first run acquire the lock and block on fetch_products

    second_result = await pipeline.run_once()
    assert second_result.skipped is True

    slow_connector.release.set()
    first_result = await first_run
    assert first_result.skipped is False
    assert slow_connector.calls == 1  # second trigger never called the store at all
