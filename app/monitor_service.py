"""Goal 2: configurable per-store monitoring. Ties together the store
registry, the cheap content+DOM-hash change check, the full LangGraph audit
pipeline, delta-report generation, and the scheduler backend - all behind
plain async methods so `monitor.py` (CLI) and any future API layer can call
into the exact same logic.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import Browser
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from typing import Awaitable, Callable

from app.change_detection import compute_content_hash, compute_dom_hash
from app.config import Settings
from app.db import AuditRun, Database, MonitoredStore, PageSnapshot
from app.fetch import PageFetcher
from app.graph import run_audit, run_audit_streaming
from app.llm.cache import LLMCache
from app.models import Finding
from app.policy_watcher import PolicyChangeResult, check_policy_sources
from app.report import generate_delta_report, generate_markdown_report, safe_host_for_filename
from app.scheduling import SchedulerBackend
from app.security.ssrf_guard import assert_public_url

logger = logging.getLogger("gmc_audit.monitor_service")

_VALID_MODES = ("interval", "on_change", "both")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MonitorService:
    def __init__(self, db: Database, scheduler: SchedulerBackend, settings: Settings, browser: Browser) -> None:
        self.db = db
        self.scheduler = scheduler
        self.settings = settings
        self.browser = browser
        # Shared across every run_full_audit call - this is what makes a
        # scheduled re-audit of an unchanged store cheap (hardening round,
        # section 1.2): unchanged page content -> identical prompt -> cache
        # hit -> no new LLM/vision API call.
        self.llm_cache = LLMCache(db)

    # --- Registration ---------------------------------------------------

    async def register_store(
        self,
        url: str,
        mode: str,
        interval_days: int | None = None,
        cheap_check_interval_days: int | None = None,
        wc_consumer_key: str | None = None,
        wc_consumer_secret: str | None = None,
        on_policy_change: bool = False,
    ) -> MonitoredStore:
        if mode not in _VALID_MODES:
            raise ValueError(f"mode must be one of {_VALID_MODES}, got {mode!r}")
        if mode in ("interval", "both") and not interval_days:
            raise ValueError("interval_days is required for interval/both mode")
        if mode in ("on_change", "both") and not cheap_check_interval_days:
            raise ValueError("cheap_check_interval_days is required for on_change/both mode")

        # Registering a store means it gets fetched on a recurring schedule
        # indefinitely - a private/internal URL here is a persistent SSRF
        # vector, worse than a one-off audit. Raises SSRFBlockedError.
        normalized_url = url if "://" in url else f"https://{url}"
        await assert_public_url(normalized_url)

        async with self.db.session() as session:
            store = MonitoredStore(
                url=url, mode=mode, interval_days=interval_days,
                cheap_check_interval_days=cheap_check_interval_days,
                wc_consumer_key=wc_consumer_key, wc_consumer_secret=wc_consumer_secret,
                on_policy_change=on_policy_change,
            )
            session.add(store)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ValueError(f"A store with url={url!r} is already registered.") from exc
            await session.refresh(store)

        self.schedule_store(store)
        logger.info("Registered store %d: %s (mode=%s)", store.id, store.url, store.mode)
        return store

    def schedule_store(self, store: MonitoredStore) -> None:
        if store.mode in ("interval", "both"):
            self.scheduler.add_interval_job(
                f"full-audit-{store.id}", self.run_full_audit, days=store.interval_days,
                store_id=store.id, trigger="interval",
            )
        if store.mode in ("on_change", "both"):
            self.scheduler.add_interval_job(
                f"cheap-check-{store.id}", self.run_cheap_check, days=store.cheap_check_interval_days,
                store_id=store.id,
            )

    def unschedule_store(self, store_id: int) -> None:
        self.scheduler.remove_job(f"full-audit-{store_id}")
        self.scheduler.remove_job(f"cheap-check-{store_id}")

    def schedule_policy_watch(self, interval_days: int) -> None:
        self.scheduler.add_interval_job("policy-watch", self.run_policy_watch, days=interval_days)

    async def list_stores(self) -> list[MonitoredStore]:
        async with self.db.session() as session:
            return list((await session.execute(select(MonitoredStore))).scalars().all())

    async def list_runs(self, store_id: int) -> list[AuditRun]:
        """Every retained AuditRun for a store, newest first - audit-history
        UI follow-up (Part 2.1). Reflects the real retention window as-is:
        old runs are already pruned by _finalize_full_audit's retention
        step, so this never needs to separately cap or explain what's
        missing - what's in the table is genuinely everything kept.
        """
        async with self.db.session() as session:
            return list((await session.execute(
                select(AuditRun).where(AuditRun.store_id == store_id).order_by(AuditRun.started_at.desc())
            )).scalars().all())

    async def remove_store(self, store_id: int) -> None:
        self.unschedule_store(store_id)
        async with self.db.session() as session:
            store = await session.get(MonitoredStore, store_id)
            if store is not None:
                await session.delete(store)
                await session.commit()

    # --- Policy watch (Goal 2.2 - independent of any store) -------------

    async def run_policy_watch(self) -> list[PolicyChangeResult]:
        results = await check_policy_sources(self.db, self.settings, self.llm_cache)
        # is_first_check is deliberately excluded here: establishing a
        # baseline the very first time a policy source is watched is not a
        # detected *change* - re-auditing every on_policy_change store on
        # day one, before any real content diff exists, would be spurious.
        changed = [r for r in results if r.changed and not r.is_first_check]
        if changed:
            logger.warning("POLICY UPDATE DETECTED for: %s - findings citing these may be stale", [r.policy_id for r in changed])
            await self._trigger_policy_change_reaudits([r.policy_id for r in changed])
        return results

    async def _trigger_policy_change_reaudits(self, changed_policy_ids: list[str]) -> None:
        """Full re-audit of every store registered for on_policy_change
        (Part 2.2, user-confirmed 2026-09-03: every such store, not just
        ones with past findings in the changed area(s) - a newly added
        requirement can affect a store that was previously clean there).
        Sequential, not gathered, to bound resource use (each is a full
        crawl + LLM grading pass); one store's failure doesn't block the
        rest, same error-isolation stance as the scheduler's own jobs.
        """
        trigger = "policy_change:" + ",".join(changed_policy_ids)
        async with self.db.session() as session:
            store_ids = (await session.execute(
                select(MonitoredStore.id).where(MonitoredStore.on_policy_change.is_(True))
            )).scalars().all()

        if not store_ids:
            return
        logger.warning("Triggering policy-change re-audit for %d store(s): %s", len(store_ids), trigger)
        for store_id in store_ids:
            try:
                await self.run_full_audit(store_id, trigger=trigger)
            except Exception:
                logger.exception("Policy-change re-audit failed for store %d - continuing with the rest", store_id)

    # --- Cheap change check (Goal 2.1, on_change mode) -------------------

    async def run_cheap_check(self, store_id: int) -> bool:
        """Fetches only the homepage (cheap - one page, not a full crawl),
        compares its content+DOM hash against the last stored snapshot, and
        triggers a full re-audit only if it differs (or this is the first
        check ever for this store). Returns whether a change was detected.
        """
        async with self.db.session() as session:
            store = await session.get(MonitoredStore, store_id)
        if store is None:
            logger.error("run_cheap_check: store %d not found", store_id)
            return False

        fetcher = PageFetcher(self.browser, max_attempts=3)
        result = await fetcher.fetch(store.url)
        if not result.ok:
            logger.warning("Cheap check fetch failed for store %d (%s): %s", store_id, store.url, result.error)
            return False

        content_hash = compute_content_hash(result.text)
        dom_hash = compute_dom_hash(result.html)

        async with self.db.session() as session:
            existing = (await session.execute(
                select(PageSnapshot).where(PageSnapshot.store_id == store_id, PageSnapshot.url == store.url)
            )).scalar_one_or_none()

            changed = existing is None or existing.content_hash != content_hash or existing.dom_hash != dom_hash
            if existing is None:
                session.add(PageSnapshot(store_id=store_id, url=store.url, content_hash=content_hash, dom_hash=dom_hash))
            else:
                existing.content_hash = content_hash
                existing.dom_hash = dom_hash
                existing.fetched_at = _utcnow()

            store_row = await session.get(MonitoredStore, store_id)
            store_row.last_cheap_check_at = _utcnow()
            session.add(AuditRun(store_id=store_id, run_type="cheap_check", trigger="on_change", finished_at=_utcnow(), change_detected=changed))
            await session.commit()

        if changed:
            logger.info("Change detected for store %d (%s) - triggering full re-audit", store_id, store.url)
            await self.run_full_audit(store_id, trigger="on_change")
        else:
            logger.info("No change detected for store %d (%s)", store_id, store.url)

        return changed

    # --- Full audit + delta report (Goal 2.4) ----------------------------

    def _settings_for_store(self, store: MonitoredStore) -> Settings:
        # wc_consumer_key/secret have no field_validator/clamping attached,
        # so model_copy(update=...) is safe here - unlike crawl_max_pages
        # etc., there's no hard-cap validator for it to silently bypass.
        return self.settings.model_copy(update={
            "wc_consumer_key": store.wc_consumer_key or self.settings.wc_consumer_key,
            "wc_consumer_secret": store.wc_consumer_secret or self.settings.wc_consumer_secret,
        })

    async def run_full_audit(self, store_id: int, trigger: str = "manual") -> AuditRun:
        async with self.db.session() as session:
            store = await session.get(MonitoredStore, store_id)
        if store is None:
            raise ValueError(f"store {store_id} not found")

        run_settings = self._settings_for_store(store)
        state = await run_audit(store.url, run_settings, self.browser, self.llm_cache)
        audit_run, _delta_markdown, _delta_markdown_major_only = await self._finalize_full_audit(store, state, trigger)
        return audit_run

    async def run_full_audit_streaming(
        self, store_id: int, trigger: str, on_phase: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict:
        """Same pipeline and same AuditRun/delta-report/snapshot persistence
        as run_full_audit - only difference is using run_audit_streaming so
        a caller (the on-demand "re-run now" API route) gets live phase-by-
        phase progress exactly like a brand-new ad-hoc audit, instead of one
        opaque "running" state. Returns a plain dict (not an AuditRun row)
        shaped to drop directly into an AuditJob's fields: report_markdown
        is the delta report when a previous run exists to diff against
        (per the "show what changed" requirement), else the fresh full
        report - the "first run" case has nothing to diff.
        """
        async with self.db.session() as session:
            store = await session.get(MonitoredStore, store_id)
        if store is None:
            raise ValueError(f"store {store_id} not found")

        run_settings = self._settings_for_store(store)
        state = await run_audit_streaming(store.url, run_settings, self.browser, self.llm_cache, on_phase)
        audit_run, delta_markdown, delta_markdown_major_only = await self._finalize_full_audit(store, state, trigger)

        return {
            "platform": state["platform"].platform.value,
            "pages_crawled": len(state["site_map"].pages),
            "findings": state.get("findings", []),
            "report_markdown": delta_markdown or audit_run.report_markdown,
            "report_markdown_major_only": delta_markdown_major_only or audit_run.report_markdown_major_only,
            "is_delta": delta_markdown is not None,
        }

    async def _finalize_full_audit(self, store: MonitoredStore, state: dict, trigger: str) -> tuple[AuditRun, str | None, str | None]:
        """Shared by run_full_audit and run_full_audit_streaming: persist
        page snapshots, compute a delta against the previous full-audit run
        (if any), write the new AuditRun row, prune old ones per the
        retention policy, and write both reports to disk. Returns the new
        AuditRun, the delta markdown, and the major-only delta markdown
        (both None if this was the first run).
        """
        store_id = store.id
        site_map = state["site_map"]
        findings: list[Finding] = state.get("findings", [])
        platform = state["platform"]
        report_markdown: str = state["report_markdown"]
        report_markdown_major_only = generate_markdown_report(platform, site_map, findings, major_only=True)

        async with self.db.session() as session:
            for page in site_map.pages:
                if not page.reachable:
                    continue
                content_hash = compute_content_hash(page.text)
                dom_hash = compute_dom_hash(page.html)
                existing = (await session.execute(
                    select(PageSnapshot).where(PageSnapshot.store_id == store_id, PageSnapshot.url == page.url)
                )).scalar_one_or_none()
                if existing is None:
                    session.add(PageSnapshot(store_id=store_id, url=page.url, content_hash=content_hash, dom_hash=dom_hash))
                else:
                    existing.content_hash = content_hash
                    existing.dom_hash = dom_hash
                    existing.fetched_at = _utcnow()

            previous_run = (await session.execute(
                select(AuditRun)
                .where(AuditRun.store_id == store_id, AuditRun.run_type == "full", AuditRun.findings_json.is_not(None))
                .order_by(AuditRun.started_at.desc())
            )).scalars().first()

            delta_markdown: str | None = None
            delta_markdown_major_only: str | None = None
            if previous_run is not None and previous_run.findings_json:
                previous_findings = [Finding.model_validate(d) for d in json.loads(previous_run.findings_json)]
                delta_markdown = generate_delta_report(platform, site_map, previous_findings, findings)
                delta_markdown_major_only = generate_delta_report(platform, site_map, previous_findings, findings, major_only=True)

            audit_run = AuditRun(
                store_id=store_id, run_type="full", trigger=trigger,
                finished_at=_utcnow(),
                report_markdown=report_markdown,
                report_markdown_major_only=report_markdown_major_only,
                findings_json=json.dumps([f.model_dump(mode="json") for f in findings]),
                change_detected=True,
                delta_markdown=delta_markdown,
                delta_markdown_major_only=delta_markdown_major_only,
            )
            session.add(audit_run)

            store_row = await session.get(MonitoredStore, store_id)
            store_row.last_full_audit_at = _utcnow()

            await session.commit()
            await session.refresh(audit_run)

            # Retention: keep only the most recent N full-audit runs for this
            # store (delta reports only ever need the single latest one, so
            # this never breaks that) - otherwise audit_runs grows without
            # bound for a long-lived monitored store.
            old_run_ids = (await session.execute(
                select(AuditRun.id)
                .where(AuditRun.store_id == store_id, AuditRun.run_type == "full")
                .order_by(AuditRun.started_at.desc())
                .offset(self.settings.audit_run_retention_count)
            )).scalars().all()
            if old_run_ids:
                for run_id in old_run_ids:
                    old_run = await session.get(AuditRun, run_id)
                    if old_run is not None:
                        await session.delete(old_run)
                await session.commit()

        self._write_reports_to_disk(store, report_markdown, delta_markdown)
        return audit_run, delta_markdown, delta_markdown_major_only

    def _write_reports_to_disk(self, store: MonitoredStore, report_markdown: str, delta_markdown: str | None) -> None:
        output_dir = Path(self.settings.report_output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        host = safe_host_for_filename(store.url) or f"store-{store.id}"
        timestamp = _utcnow().strftime("%Y%m%d-%H%M%S")

        (output_dir / f"{host}-{timestamp}.md").write_text(report_markdown, encoding="utf-8")
        if delta_markdown:
            (output_dir / f"{host}-{timestamp}-delta.md").write_text(delta_markdown, encoding="utf-8")
