"""Persistence layer for the monitoring subsystem (Goal 2): registered
stores, per-page content/DOM snapshots for change detection, audit run
history (for delta reports), and policy-source snapshots (for independent
policy-update detection).

Defaults to a local `sqlite+aiosqlite` file so registering/running a monitor
needs zero setup - point `DATABASE_URL` at `postgresql+asyncpg://...` for
production; these are plain SQLAlchemy models, portable across both. Every
datetime column is declared `DateTime(timezone=True)` deliberately, not
left to the default mapping: every datetime this app produces (`_utcnow()`
below) is timezone-aware UTC, and Postgres's default `TIMESTAMP WITHOUT
TIME ZONE` column type rejects a tz-aware value outright (asyncpg raises
"can't subtract offset-naive and offset-aware datetimes") - SQLite doesn't
enforce this at all, so the mismatch was invisible in local/SQLite dev and
only surfaced live against a real Postgres database. Confirmed live, not
hypothetical.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class MonitoredStore(Base):
    __tablename__ = "monitored_stores"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(unique=True)
    platform_hint: Mapped[str | None] = mapped_column(default=None)
    wc_consumer_key: Mapped[str | None] = mapped_column(default=None)
    wc_consumer_secret: Mapped[str | None] = mapped_column(default=None)

    # "interval" | "on_change" | "both"
    mode: Mapped[str] = mapped_column(default="interval")
    interval_days: Mapped[int | None] = mapped_column(default=None)
    cheap_check_interval_days: Mapped[int | None] = mapped_column(default=None)
    # Independent of `mode` above, not a third value of it (audit-history +
    # policy-change-triggered re-audits follow-up, Part 2.2) - a store can be
    # on_change/interval/both AND opted into policy-change re-audits at the
    # same time. Kept as its own column rather than folding into `mode`'s
    # string enum so the existing mode values/logic never have to change.
    on_policy_change: Mapped[bool] = mapped_column(default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_full_audit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_cheap_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class PageSnapshot(Base):
    """Content+DOM hash for one (store, url) pair, used by the cheap
    change-detection check to decide whether a full re-audit is warranted.
    """
    __tablename__ = "page_snapshots"
    __table_args__ = (UniqueConstraint("store_id", "url", name="uq_page_snapshot_store_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("monitored_stores.id"))
    url: Mapped[str]
    content_hash: Mapped[str]
    dom_hash: Mapped[str]
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AuditRun(Base):
    """One pipeline run (full audit or cheap check) against a monitored
    store. `findings_json` holds the serialized Finding list so the next
    run can diff against it for a delta report.
    """
    __tablename__ = "audit_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("monitored_stores.id"))
    run_type: Mapped[str]  # "full" | "cheap_check"
    trigger: Mapped[str]  # "interval" | "on_change" | "manual"
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    report_markdown: Mapped[str | None] = mapped_column(default=None)
    # Same report, filtered to Critical/High (real GMC suspension-risk)
    # findings only - computed alongside report_markdown at generation time
    # (needs the full site_map, which isn't persisted, so it can't be
    # derived later from findings_json alone) so the "major issues only"
    # toggle never re-crawls or re-runs LLM grading just to change severity
    # filtering.
    report_markdown_major_only: Mapped[str | None] = mapped_column(default=None)
    findings_json: Mapped[str | None] = mapped_column(default=None)
    change_detected: Mapped[bool] = mapped_column(default=False)
    # Delta vs. the previous full-audit run for this store, computed once at
    # run time (MonitorService._finalize_full_audit already builds this to
    # write alongside the report file) and persisted here too (audit-history
    # UI follow-up, Part 2.1) so the history view can show "what changed vs.
    # the previous run" for any retained run, not just the most recent one -
    # generating a delta after the fact isn't possible from history alone,
    # since it needs the full site_map/platform context that only exists at
    # audit time and is never itself persisted. None for a store's first run
    # (nothing to diff against) or a cheap_check run (no delta concept).
    delta_markdown: Mapped[str | None] = mapped_column(default=None)
    delta_markdown_major_only: Mapped[str | None] = mapped_column(default=None)


class PolicySourceSnapshot(Base):
    """Hash of one real GMC Help Center policy page, checked independently
    of any store's monitoring schedule (Goal 2.2).
    """
    __tablename__ = "policy_source_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    policy_id: Mapped[str] = mapped_column(unique=True)
    source_url: Mapped[str]
    content_hash: Mapped[str]
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AuditJobRecord(Base):
    """Persisted state for a "Run Audit" job - both an ad-hoc one started
    from the frontend's Home screen, and an on-demand "re-run now" for an
    already-monitored store (not tied to a store row itself; that store's
    own AuditRun history is what the on-demand re-run reads/writes -
    see MonitorService.run_full_audit_streaming). Status, phase, findings,
    and the rendered report itself all live here, not just in process
    memory, so a backend restart doesn't orphan a job's download links or
    make an in-progress poll hang forever.

    Retention: no automatic cleanup yet. These are small text/JSON blobs
    (report_markdown is typically under a few MB) and ad-hoc audits are a
    manual, rate-limited action - not worth building a sweep job for at this
    scale. Revisit if this table's row count or size ever becomes a real
    concern; `created_at` is already indexed via the primary scan pattern
    needed for a future "delete older than N days" job.
    """
    __tablename__ = "audit_jobs"

    job_id: Mapped[str] = mapped_column(primary_key=True)
    url: Mapped[str]
    status: Mapped[str] = mapped_column(default="pending")  # pending | running | done | error
    phase: Mapped[str | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(default=None)
    platform: Mapped[str | None] = mapped_column(default=None)
    pages_crawled: Mapped[int | None] = mapped_column(default=None)
    findings_json: Mapped[str | None] = mapped_column(default=None)
    report_markdown: Mapped[str | None] = mapped_column(default=None)
    # See AuditRun.report_markdown_major_only - same idea, same reason it's
    # computed and stored up front rather than derived on request.
    report_markdown_major_only: Mapped[str | None] = mapped_column(default=None)
    # True when report_markdown is a delta report (changes since the
    # store's previous run) rather than a full report - only ever set for
    # on-demand store re-runs that had a previous run to diff against.
    is_delta: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PolicyChunk(Base):
    """One chunk of real, live-scraped GMC Help Center policy text, plus its
    embedding (Phase C - replaces the hand-written stub summaries in
    app/llm/policy_snippets.py). embedding_json is a JSON-encoded
    list[float], not a native pgvector column: this corpus is a few hundred
    chunks at most (8 policy areas x a handful of real source pages each),
    so a full Python-side cosine-similarity scan is effectively instant and
    doesn't need a real ANN vector index or a hard Postgres+pgvector
    dependency - see app/llm/policy_rag.py for the retrieval side.

    Rebuilding a policy_id's index deletes and re-inserts all of its rows
    (see rebuild_policy_index) - simplest correct approach at this scale,
    no need for incremental chunk-level diffing.
    """
    __tablename__ = "policy_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    policy_id: Mapped[str]
    source_url: Mapped[str]
    section: Mapped[str]
    chunk_index: Mapped[int]
    chunk_text: Mapped[str]
    embedding_json: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class LLMCacheEntry(Base):
    """Content-hash-keyed cache of an LLM/vision grading result (section 1
    of the hardening round). cache_key is sha256(provider|model|tool_name|
    content_signature) - content_signature is the exact page text sent (text
    checks) or the image URL (vision checks), so any content change produces
    a different key naturally, with no separate invalidation step needed.
    """
    __tablename__ = "llm_cache_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    cache_key: Mapped[str] = mapped_column(unique=True)
    result_json: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


def _add_missing_columns(sync_conn) -> None:
    """`Base.metadata.create_all` only creates tables that don't exist yet -
    it never alters an existing table to add a newly-declared column (e.g.
    AuditJobRecord.is_delta, added after this project already had a real
    gmc_monitor.db on disk with monitored stores and job history in it).
    Without this, adding a column to a model is a silent runtime break
    ("no such column") for anyone with an existing DB file - and deleting/
    recreating the DB isn't an acceptable fix, since that's real user data.

    Deliberately minimal - handles the one schema-evolution shape this
    project actually needs (add a nullable-with-a-Python-default column),
    not a general migration framework. Backfills existing rows to the
    column's default (bound as a parameter, not string-formatted, so this
    is safe regardless of the default's type) so old rows don't end up with
    a NULL where the model declares a non-optional type.
    """
    inspector = inspect(sync_conn)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing_columns = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing_columns:
                continue
            col_type = column.type.compile(dialect=sync_conn.dialect)
            sync_conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type}"))
            if column.default is not None and column.default.is_scalar:
                sync_conn.execute(
                    text(f"UPDATE {table.name} SET {column.name} = :default_value WHERE {column.name} IS NULL"),
                    {"default_value": column.default.arg},
                )


class Database:
    def __init__(self, database_url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(database_url, echo=False)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_add_missing_columns)

    def session(self) -> AsyncSession:
        return self.sessionmaker()

    async def dispose(self) -> None:
        await self.engine.dispose()
