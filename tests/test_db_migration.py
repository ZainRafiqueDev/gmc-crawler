"""Confirms the new columns added this round (MonitoredStore.on_policy_change,
AuditRun.delta_markdown/delta_markdown_major_only - audit-history + policy-
change-triggered re-audits follow-up) are correctly auto-migrated onto an
existing, older-schema DB file by app.db's _add_missing_columns mechanism -
not just correctly declared on a brand-new DB (which create_all alone would
already handle, so wouldn't actually exercise the migration path this
project's real deployments depend on for anyone with an existing gmc_monitor.db).
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db import AuditRun, Database, MonitoredStore


@pytest.mark.asyncio
async def test_new_columns_are_added_to_a_pre_existing_older_schema_db(tmp_path):
    db_path = tmp_path / "old_schema.db"
    database = Database(f"sqlite+aiosqlite:///{db_path}")

    # Simulate a DB created before this round's columns existed: create the
    # tables with the old, narrower schema by hand, then insert a row -
    # real pre-existing user data that a migration must not lose or block on.
    async with database.engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE monitored_stores ("
            "id INTEGER PRIMARY KEY, url VARCHAR UNIQUE, platform_hint VARCHAR, "
            "wc_consumer_key VARCHAR, wc_consumer_secret VARCHAR, mode VARCHAR, "
            "interval_days INTEGER, cheap_check_interval_days INTEGER, "
            "created_at DATETIME, last_full_audit_at DATETIME, last_cheap_check_at DATETIME)"
        ))
        await conn.execute(text(
            "INSERT INTO monitored_stores (id, url, mode, created_at) VALUES (1, 'https://old.example/', 'interval', '2026-01-01 00:00:00')"
        ))
        await conn.execute(text(
            "CREATE TABLE audit_runs ("
            "id INTEGER PRIMARY KEY, store_id INTEGER, run_type VARCHAR, trigger VARCHAR, "
            "started_at DATETIME, finished_at DATETIME, report_markdown TEXT, "
            "report_markdown_major_only TEXT, findings_json TEXT, change_detected BOOLEAN)"
        ))
        await conn.execute(text(
            "INSERT INTO audit_runs (id, store_id, run_type, trigger, started_at, change_detected) "
            "VALUES (1, 1, 'full', 'manual', '2026-01-01 00:00:00', 1)"
        ))
    await database.dispose()

    # Re-open through the real Database.init() path - this is what every
    # real deployment's startup does, and what must apply the migration.
    database2 = Database(f"sqlite+aiosqlite:///{db_path}")
    await database2.init()

    async with database2.session() as session:
        store = await session.get(MonitoredStore, 1)
        run = await session.get(AuditRun, 1)

    assert store is not None
    assert store.url == "https://old.example/"  # pre-existing data survived
    assert store.on_policy_change is False  # new column, backfilled to its default

    assert run is not None
    assert run.trigger == "manual"  # pre-existing data survived
    assert run.delta_markdown is None
    assert run.delta_markdown_major_only is None

    await database2.dispose()
