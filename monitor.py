#!/usr/bin/env python
"""Goal 2 CLI: register stores for recurring monitoring and run the
scheduler daemon. Separate from `audit.py` (the one-shot Phase 1 CLI)
because this is a different mode of operation - persistent state, a
long-running process, delta reports - not a single audit run.

    python monitor.py register --url https://example.com --mode interval --interval-days 3
    python monitor.py register --url https://example.com --mode on_change --cheap-check-interval-days 1
    python monitor.py list
    python monitor.py run-full --store-id 1
    python monitor.py run-cheap-check --store-id 1
    python monitor.py policy-check
    python monitor.py serve
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from playwright.async_api import async_playwright

from app.config import load_settings
from app.db import Database
from app.monitor_service import MonitorService
from app.scheduling import APSchedulerBackend
from app.security.ssrf_guard import SSRFBlockedError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("gmc_audit.monitor_cli")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Configurable GMC compliance monitoring.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_register = sub.add_parser("register", help="Register a store for recurring monitoring.")
    p_register.add_argument("--url", required=True)
    p_register.add_argument("--mode", required=True, choices=["interval", "on_change", "both"])
    p_register.add_argument("--interval-days", type=int, default=None)
    p_register.add_argument("--cheap-check-interval-days", type=int, default=None)
    p_register.add_argument("--wc-key", default=None)
    p_register.add_argument("--wc-secret", default=None)

    sub.add_parser("list", help="List registered stores.")

    p_run_full = sub.add_parser("run-full", help="Manually trigger a full audit for one store now.")
    p_run_full.add_argument("--store-id", type=int, required=True)

    p_run_cheap = sub.add_parser("run-cheap-check", help="Manually trigger a cheap change check for one store now.")
    p_run_cheap.add_argument("--store-id", type=int, required=True)

    sub.add_parser("policy-check", help="Manually trigger the independent GMC policy-source change check.")

    p_remove = sub.add_parser("remove", help="Unregister a store and stop monitoring it.")
    p_remove.add_argument("--store-id", type=int, required=True)

    p_serve = sub.add_parser("serve", help="Run the scheduler daemon in the foreground (blocking).")
    p_serve.add_argument("--policy-check-interval-days", type=int, default=None, help="Overrides DEFAULT_POLICY_WATCH_INTERVAL_DAYS")

    return parser.parse_args(argv)


async def _build_service(settings, browser) -> MonitorService:
    db = Database(settings.database_url)
    await db.init()
    scheduler = APSchedulerBackend()
    return MonitorService(db=db, scheduler=scheduler, settings=settings, browser=browser)


async def main_async(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings()

    async with async_playwright() as pw:
        # See app/api/main.py's lifespan for why --disable-dev-shm-usage matters
        # in a memory-constrained container.
        browser = await pw.chromium.launch(args=["--disable-dev-shm-usage"])
        try:
            service = await _build_service(settings, browser)

            if args.command == "register":
                try:
                    store = await service.register_store(
                        url=args.url, mode=args.mode,
                        interval_days=args.interval_days, cheap_check_interval_days=args.cheap_check_interval_days,
                        wc_consumer_key=args.wc_key, wc_consumer_secret=args.wc_secret,
                    )
                except (ValueError, SSRFBlockedError) as exc:
                    print(f"Error: {exc}")
                    await service.db.dispose()
                    return 1
                print(f"Registered store {store.id}: {store.url} (mode={store.mode})")

            elif args.command == "list":
                stores = await service.list_stores()
                if not stores:
                    print("No stores registered.")
                for s in stores:
                    print(f"[{s.id}] {s.url} mode={s.mode} interval_days={s.interval_days} "
                          f"cheap_check_interval_days={s.cheap_check_interval_days} "
                          f"last_full_audit_at={s.last_full_audit_at} last_cheap_check_at={s.last_cheap_check_at}")

            elif args.command == "run-full":
                audit_run = await service.run_full_audit(args.store_id, trigger="manual")
                print(f"Full audit complete for store {args.store_id} (audit_run_id={audit_run.id}). Reports written to {settings.report_output_dir}")

            elif args.command == "run-cheap-check":
                changed = await service.run_cheap_check(args.store_id)
                print(f"Cheap check complete for store {args.store_id}. Change detected: {changed}")

            elif args.command == "policy-check":
                results = await service.run_policy_watch()
                for r in results:
                    status = "CHANGED" if r.changed else ("baseline recorded" if r.is_first_check else "unchanged")
                    print(f"  {r.policy_id}: {status} ({r.source_url})")

            elif args.command == "remove":
                await service.remove_store(args.store_id)
                print(f"Removed store {args.store_id}.")

            elif args.command == "serve":
                stores = await service.list_stores()
                for store in stores:
                    service.schedule_store(store)
                service.schedule_policy_watch(args.policy_check_interval_days or settings.default_policy_watch_interval_days)
                service.scheduler.start()
                logger.info("Scheduler started with %d store(s) registered. Press Ctrl+C to stop.", len(stores))
                try:
                    await asyncio.Event().wait()
                except (KeyboardInterrupt, asyncio.CancelledError):
                    pass
                finally:
                    service.scheduler.shutdown()

            await service.db.dispose()
        finally:
            await browser.close()

    return 0


def main() -> None:
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
