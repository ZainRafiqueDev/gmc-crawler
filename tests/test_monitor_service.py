from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings
from app.db import AuditRun, Database, PageSnapshot
from app.fetch import FetchResult
from app.models import Confidence, Finding, Platform, PlatformDetectionResult, Severity, SiteMap
from app.monitor_service import MonitorService
from app.scheduling import SchedulerBackend
from app.security import ssrf_guard as ssrf_guard_module

# Captured at import time, before any fixture (this file's or conftest's
# global one) has a chance to monkeypatch app.security.ssrf_guard's own
# attribute - this is the real, unpatched function.
_real_assert_public_url = ssrf_guard_module.assert_public_url


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    """monitor_service.py imports assert_public_url directly, so the global
    conftest fake (which patches the ssrf_guard module's own attribute)
    doesn't reach this binding - neutralize it here too. These tests use
    fake domains (x.example) and must stay network-free.
    """
    async def fake_assert_public_url(url):
        return None
    monkeypatch.setattr("app.monitor_service.assert_public_url", fake_assert_public_url)


class FakeScheduler:
    def __init__(self):
        self.jobs: dict[str, dict] = {}

    def add_interval_job(self, job_id, func, days, **kwargs):
        self.jobs[job_id] = {"func": func, "days": days, "kwargs": kwargs}

    def remove_job(self, job_id):
        self.jobs.pop(job_id, None)

    def has_job(self, job_id):
        return job_id in self.jobs

    def start(self):
        pass

    def shutdown(self):
        pass


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/monitor_test.db")
    await database.init()
    yield database
    await database.dispose()


@pytest.fixture
def service(db, tmp_path):
    settings = Settings(report_output_dir=tmp_path / "reports")
    scheduler = FakeScheduler()
    browser = MagicMock()
    return MonitorService(db=db, scheduler=scheduler, settings=settings, browser=browser)


def _audit_state(findings: list[Finding]):
    site_map = SiteMap(base_url="https://x.example/", pages=[])
    platform = PlatformDetectionResult(platform=Platform.WOOCOMMERCE, base_url="https://x.example/", evidence=[])
    return {"site_map": site_map, "platform": platform, "findings": findings, "report_markdown": "# Report\n"}


def _finding(check_id="https_enforced", evidence="e1"):
    return Finding(check_id=check_id, title=check_id, severity=Severity.HIGH, confidence=Confidence.CONFIRMED, evidence=evidence)


# --- Registration / scheduling ---------------------------------------------

@pytest.mark.asyncio
async def test_register_interval_mode_schedules_only_full_audit_job(service):
    store = await service.register_store("https://x.example/", mode="interval", interval_days=3)
    assert f"full-audit-{store.id}" in service.scheduler.jobs
    assert f"cheap-check-{store.id}" not in service.scheduler.jobs
    assert service.scheduler.jobs[f"full-audit-{store.id}"]["days"] == 3


@pytest.mark.asyncio
async def test_register_on_change_mode_schedules_only_cheap_check_job(service):
    store = await service.register_store("https://x.example/", mode="on_change", cheap_check_interval_days=1)
    assert f"cheap-check-{store.id}" in service.scheduler.jobs
    assert f"full-audit-{store.id}" not in service.scheduler.jobs


@pytest.mark.asyncio
async def test_register_both_mode_schedules_both_jobs(service):
    store = await service.register_store("https://x.example/", mode="both", interval_days=7, cheap_check_interval_days=1)
    assert f"full-audit-{store.id}" in service.scheduler.jobs
    assert f"cheap-check-{store.id}" in service.scheduler.jobs


@pytest.mark.asyncio
async def test_register_blocked_private_url_raises_ssrf_error(service, monkeypatch):
    from app.security.ssrf_guard import SSRFBlockedError
    monkeypatch.setattr("app.monitor_service.assert_public_url", _real_assert_public_url)

    with pytest.raises(SSRFBlockedError):
        await service.register_store("http://169.254.169.254/", mode="interval", interval_days=3)


@pytest.mark.asyncio
async def test_register_invalid_mode_raises(service):
    with pytest.raises(ValueError):
        await service.register_store("https://x.example/", mode="bogus")


@pytest.mark.asyncio
async def test_register_duplicate_url_raises_clean_value_error(service):
    await service.register_store("https://x.example/", mode="interval", interval_days=3)
    with pytest.raises(ValueError, match="already registered"):
        await service.register_store("https://x.example/", mode="interval", interval_days=7)


@pytest.mark.asyncio
async def test_register_interval_mode_without_interval_days_raises(service):
    with pytest.raises(ValueError):
        await service.register_store("https://x.example/", mode="interval")


@pytest.mark.asyncio
async def test_remove_store_unschedules_jobs(service):
    store = await service.register_store("https://x.example/", mode="both", interval_days=7, cheap_check_interval_days=1)
    await service.remove_store(store.id)
    assert f"full-audit-{store.id}" not in service.scheduler.jobs
    assert f"cheap-check-{store.id}" not in service.scheduler.jobs
    assert await service.list_stores() == []


# --- Cheap check (on_change) -------------------------------------------

@pytest.mark.asyncio
async def test_cheap_check_first_run_is_a_change_and_triggers_full_audit(service):
    store = await service.register_store("https://x.example/", mode="on_change", cheap_check_interval_days=1)
    fetch_result = FetchResult(url=store.url, ok=True, status=200, html="<html><body>hi</body></html>", text="hi", final_url=store.url, attempts=1)

    with patch("app.monitor_service.PageFetcher") as MockFetcher, \
         patch.object(service, "run_full_audit", new_callable=AsyncMock) as mock_full_audit:
        MockFetcher.return_value.fetch = AsyncMock(return_value=fetch_result)
        changed = await service.run_cheap_check(store.id)

    assert changed is True
    mock_full_audit.assert_awaited_once_with(store.id, trigger="on_change")


@pytest.mark.asyncio
async def test_cheap_check_no_change_does_not_trigger_full_audit(service):
    store = await service.register_store("https://x.example/", mode="on_change", cheap_check_interval_days=1)
    fetch_result = FetchResult(url=store.url, ok=True, status=200, html="<html><body>hi</body></html>", text="hi", final_url=store.url, attempts=1)

    with patch("app.monitor_service.PageFetcher") as MockFetcher, \
         patch.object(service, "run_full_audit", new_callable=AsyncMock) as mock_full_audit:
        MockFetcher.return_value.fetch = AsyncMock(return_value=fetch_result)
        await service.run_cheap_check(store.id)  # baseline, triggers full audit
        mock_full_audit.reset_mock()
        changed = await service.run_cheap_check(store.id)  # same content again

    assert changed is False
    mock_full_audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cheap_check_detects_real_content_change_and_triggers_full_audit(service):
    store = await service.register_store("https://x.example/", mode="on_change", cheap_check_interval_days=1)
    original = FetchResult(url=store.url, ok=True, status=200, html="<html><body>Sale ends Monday</body></html>", text="Sale ends Monday", final_url=store.url, attempts=1)
    modified = FetchResult(url=store.url, ok=True, status=200, html="<html><body>Sale ends Friday</body></html>", text="Sale ends Friday", final_url=store.url, attempts=1)

    with patch("app.monitor_service.PageFetcher") as MockFetcher, \
         patch.object(service, "run_full_audit", new_callable=AsyncMock) as mock_full_audit:
        MockFetcher.return_value.fetch = AsyncMock(return_value=original)
        await service.run_cheap_check(store.id)
        mock_full_audit.reset_mock()

        MockFetcher.return_value.fetch = AsyncMock(return_value=modified)
        changed = await service.run_cheap_check(store.id)

    assert changed is True
    mock_full_audit.assert_awaited_once()


# --- Full audit + delta report -----------------------------------------

@pytest.mark.asyncio
async def test_full_audit_first_run_writes_report_no_delta(service, tmp_path):
    store = await service.register_store("https://x.example/", mode="interval", interval_days=3)
    findings = [_finding()]

    with patch("app.monitor_service.run_audit", new_callable=AsyncMock, return_value=_audit_state(findings)):
        audit_run = await service.run_full_audit(store.id, trigger="manual")

    assert audit_run.findings_json is not None
    assert json.loads(audit_run.findings_json)[0]["check_id"] == "https_enforced"
    md_files = list((tmp_path / "reports").glob("*.md"))
    assert len(md_files) == 1  # full report only, no delta on first run
    assert "delta" not in md_files[0].name


@pytest.mark.asyncio
async def test_full_audit_second_run_generates_delta_report(service, tmp_path):
    store = await service.register_store("https://x.example/", mode="interval", interval_days=3)

    with patch("app.monitor_service.run_audit", new_callable=AsyncMock, return_value=_audit_state([_finding(evidence="e1")])):
        await service.run_full_audit(store.id, trigger="manual")

    with patch("app.monitor_service.run_audit", new_callable=AsyncMock, return_value=_audit_state([_finding(evidence="e2"), _finding(check_id="new_check")])):
        await service.run_full_audit(store.id, trigger="interval")

    delta_files = list((tmp_path / "reports").glob("*-delta.md"))
    assert len(delta_files) == 1
    content = delta_files[0].read_text()
    assert "New issues: 1" in content
    assert "Changed issues: 1" in content


@pytest.mark.asyncio
async def test_full_audit_updates_page_snapshots(service, db):
    from app.models import CrawledPage, PageType

    store = await service.register_store("https://x.example/", mode="interval", interval_days=3)
    page = CrawledPage(url="https://x.example/", page_type=PageType.HOMEPAGE, depth=0, reachable=True, html="<html>hi</html>", text="hi")
    state = _audit_state([])
    state["site_map"] = SiteMap(base_url="https://x.example/", pages=[page])

    with patch("app.monitor_service.run_audit", new_callable=AsyncMock, return_value=state):
        await service.run_full_audit(store.id)

    async with db.session() as session:
        from sqlalchemy import select
        snapshot = (await session.execute(select(PageSnapshot).where(PageSnapshot.store_id == store.id))).scalar_one()
        assert snapshot.url == "https://x.example/"


# --- Audit history (follow-up round, Part 2.1) ---------------------------

@pytest.mark.asyncio
async def test_first_run_has_no_delta_persisted_on_the_row(service):
    store = await service.register_store("https://x.example/", mode="interval", interval_days=3)
    with patch("app.monitor_service.run_audit", new_callable=AsyncMock, return_value=_audit_state([_finding()])):
        audit_run = await service.run_full_audit(store.id, trigger="manual")
    assert audit_run.delta_markdown is None
    assert audit_run.delta_markdown_major_only is None


@pytest.mark.asyncio
async def test_second_run_persists_delta_markdown_on_the_row_not_just_disk(service):
    """The delta report was already being computed and written to disk -
    this confirms it's now ALSO persisted on the AuditRun row itself, so
    the audit-history API can serve it for any retained run, not only the
    one most recently written to disk."""
    store = await service.register_store("https://x.example/", mode="interval", interval_days=3)
    with patch("app.monitor_service.run_audit", new_callable=AsyncMock, return_value=_audit_state([_finding(evidence="e1")])):
        await service.run_full_audit(store.id, trigger="manual")
    with patch("app.monitor_service.run_audit", new_callable=AsyncMock, return_value=_audit_state([_finding(evidence="e2"), _finding(check_id="new_check")])):
        second_run = await service.run_full_audit(store.id, trigger="interval")

    assert second_run.delta_markdown is not None
    assert "New issues: 1" in second_run.delta_markdown
    assert second_run.delta_markdown_major_only is not None


@pytest.mark.asyncio
async def test_list_runs_returns_newest_first_and_reflects_retention(service):
    store = await service.register_store("https://x.example/", mode="interval", interval_days=3)
    service.settings.audit_run_retention_count = 2
    for i in range(4):
        with patch("app.monitor_service.run_audit", new_callable=AsyncMock, return_value=_audit_state([_finding(evidence=f"e{i}")])):
            await service.run_full_audit(store.id, trigger="manual")

    runs = await service.list_runs(store.id)
    assert len(runs) == 2  # retention pruned the older two - nothing implies more exist
    assert runs[0].started_at >= runs[1].started_at


# --- on_policy_change registration + trigger (follow-up round, Part 2.2) --

@pytest.mark.asyncio
async def test_register_store_persists_on_policy_change_flag(service):
    store = await service.register_store("https://x.example/", mode="interval", interval_days=3, on_policy_change=True)
    assert store.on_policy_change is True
    stores = await service.list_stores()
    assert stores[0].on_policy_change is True


@pytest.mark.asyncio
async def test_register_store_defaults_on_policy_change_to_false(service):
    store = await service.register_store("https://x.example/", mode="interval", interval_days=3)
    assert store.on_policy_change is False


@pytest.mark.asyncio
async def test_policy_watch_triggers_full_audit_for_every_on_policy_change_store(service):
    store_a = await service.register_store("https://a.example/", mode="interval", interval_days=30, on_policy_change=True)
    store_b = await service.register_store("https://b.example/", mode="interval", interval_days=30, on_policy_change=True)
    not_opted_in = await service.register_store("https://c.example/", mode="interval", interval_days=30, on_policy_change=False)

    from app.policy_watcher import PolicyChangeResult
    fake_results = [
        PolicyChangeResult(policy_id="shipping_policy", source_urls=["https://x"], changed=True, is_first_check=False, current_hash="h2", previous_hash="h1"),
        PolicyChangeResult(policy_id="returns_refunds", source_urls=["https://y"], changed=False, is_first_check=False, current_hash="h1"),
    ]
    with patch("app.monitor_service.check_policy_sources", new_callable=AsyncMock, return_value=fake_results), \
         patch.object(service, "run_full_audit", new_callable=AsyncMock) as mock_full_audit:
        await service.run_policy_watch()

    audited_store_ids = {call.args[0] for call in mock_full_audit.await_args_list}
    assert audited_store_ids == {store_a.id, store_b.id}
    assert not_opted_in.id not in audited_store_ids
    for call in mock_full_audit.await_args_list:
        assert call.kwargs["trigger"] == "policy_change:shipping_policy"


@pytest.mark.asyncio
async def test_policy_watch_first_check_baseline_does_not_trigger_reaudits(service):
    """Establishing a baseline (is_first_check=True) is not a detected
    change - re-auditing on_policy_change stores before any real content
    diff exists would be spurious."""
    store = await service.register_store("https://a.example/", mode="interval", interval_days=30, on_policy_change=True)

    from app.policy_watcher import PolicyChangeResult
    fake_results = [
        PolicyChangeResult(policy_id="shipping_policy", source_urls=["https://x"], changed=False, is_first_check=True, current_hash="h1"),
    ]
    with patch("app.monitor_service.check_policy_sources", new_callable=AsyncMock, return_value=fake_results), \
         patch.object(service, "run_full_audit", new_callable=AsyncMock) as mock_full_audit:
        await service.run_policy_watch()

    mock_full_audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_policy_watch_no_changes_does_not_trigger_reaudits(service):
    await service.register_store("https://a.example/", mode="interval", interval_days=30, on_policy_change=True)

    from app.policy_watcher import PolicyChangeResult
    fake_results = [
        PolicyChangeResult(policy_id="shipping_policy", source_urls=["https://x"], changed=False, is_first_check=False, current_hash="h1", previous_hash="h1"),
    ]
    with patch("app.monitor_service.check_policy_sources", new_callable=AsyncMock, return_value=fake_results), \
         patch.object(service, "run_full_audit", new_callable=AsyncMock) as mock_full_audit:
        await service.run_policy_watch()

    mock_full_audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_policy_watch_one_store_failure_does_not_block_the_rest(service):
    store_a = await service.register_store("https://a.example/", mode="interval", interval_days=30, on_policy_change=True)
    store_b = await service.register_store("https://b.example/", mode="interval", interval_days=30, on_policy_change=True)

    from app.policy_watcher import PolicyChangeResult
    fake_results = [PolicyChangeResult(policy_id="shipping_policy", source_urls=["https://x"], changed=True, is_first_check=False, current_hash="h2", previous_hash="h1")]

    async def flaky_full_audit(store_id, trigger="manual"):
        if store_id == store_a.id:
            raise RuntimeError("simulated crawl failure")
        return None

    with patch("app.monitor_service.check_policy_sources", new_callable=AsyncMock, return_value=fake_results), \
         patch.object(service, "run_full_audit", side_effect=flaky_full_audit) as mock_full_audit:
        await service.run_policy_watch()  # must not raise

    audited_store_ids = {call.args[0] for call in mock_full_audit.await_args_list}
    assert audited_store_ids == {store_a.id, store_b.id}  # both attempted despite the first failing
