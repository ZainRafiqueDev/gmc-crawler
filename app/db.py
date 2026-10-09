"""Persistence layer for the monitoring subsystem (Goal 2): registered
stores, per-page content/DOM snapshots for change detection, audit run
history (for delta reports), and policy-source snapshots (for independent
policy-update detection). Also holds the standalone First Audit pipeline's
evidence store (FirstAuditRun, Resource, ResourceFact, Product, RuleRecord,
Evaluation, FindingRecord) - a separate table set, not wired into the
monitoring tables above (see FirstAuditRun's docstring).

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


class FirstAuditRun(Base):
    """One run of the standalone "First Audit" pipeline (GMC bot follow-up,
    work-order step 3) - the one-time expensive initial scan for a single
    store URL. Deliberately NOT the same table as AuditRun above: AuditRun is
    scoped to the monitoring subsystem (requires a store_id, feeds the
    scheduler's delta-report history); this is a standalone run that may
    exist with no monitored store at all.

    store_id is a nullable seam for linking a first audit to a monitored
    store later - never populated by this round's flow (first audit stays
    fully standalone per explicit instruction; monitoring wiring is future
    work, not built here).
    """
    __tablename__ = "first_audit_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str]
    store_id: Mapped[int | None] = mapped_column(ForeignKey("monitored_stores.id"), default=None)
    status: Mapped[str] = mapped_column(default="pending")  # pending | running | done | error
    platform: Mapped[str | None] = mapped_column(default=None)
    pages_crawled: Mapped[int | None] = mapped_column(default=None)
    # Count of crawled pages that were blocked/unreachable (CrawledPage.
    # cannot_verify - a bot-protection block, network error, etc. - never a
    # confirmed 404/410 or an SSRF refusal, both of which are genuine
    # negatives, not read failures). Surfaced run-wide (PDF header, etc.) so
    # a compliance status is never silently declared without disclosing how
    # much of the site could actually be read - "couldn't read it" must
    # never look identical to "confirmed clean."
    unreachable_pages_count: Mapped[int | None] = mapped_column(default=None)
    # COMPLIANT | ACTION_REQUIRED | AT_RISK | CRITICAL - populated once the
    # rule engine + snapshot rollup run (work-order step 4), None until then.
    snapshot_status: Mapped[str | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(default=None)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Blob(Base):
    """Tier 2 (cold) evidence storage metadata - the actual bytes live in
    object storage (app.evidence_storage), never in this row. `hash` is both
    the primary key and the content-addressable storage key: identical
    content (an unchanged page re-crawled on a later run, or the same
    boilerplate JSON-LD block on two different products) is uploaded once and
    referenced by every Resource/FindingRecord that points at it, verified by
    app.evidence_blob_store.write_blob_if_needed's dedup-on-write path.

    refcount is the number of Resource/FindingRecord rows currently pointing
    at this hash - app.evidence_blob_store.release_blob decrements it and
    deletes both this row and the stored object once it reaches zero.
    last_referenced_at is bumped on every write-path call that resolves to
    this existing hash (a fresh "this still matters" signal independent of
    created_at), used by the retention job's age-out decision.
    """
    __tablename__ = "blobs"

    hash: Mapped[str] = mapped_column(primary_key=True)  # sha256 of the UNCOMPRESSED content
    storage_key: Mapped[str]  # backend-specific key/path; local and s3 adapters both key by hash
    content_type: Mapped[str]
    bytes: Mapped[int]  # compressed, on-disk/on-bucket size
    codec: Mapped[str]  # "zstd" | "gzip" | "none" - needed to decompress on read
    refcount: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_referenced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Resource(Base):
    """Evidence-store record for one crawled URL in one FirstAuditRun (spec
    section 2.7) - the persisted counterpart of app.models.CrawledPage.
    Deliberately does not replace CrawledPage/SiteMap: those stay the
    in-memory shape the existing crawl/classify/check pipeline already uses
    (reused as-is, per instruction); this table is the durable record of what
    was found, for the admin UI and for cross-run evidence.

    discovered_via is best-effort ("homepage" | "crawl") rather than a
    precise sitemap/nav/footer distinction - app.site_mapper.CrawledPage
    doesn't currently track which discovery path found a URL, and extending
    it to do so would mean modifying site_mapper.py, which this round's
    instructions explicitly say to reuse as-is rather than rewrite.
    """
    __tablename__ = "resources"

    id: Mapped[int] = mapped_column(primary_key=True)
    audit_run_id: Mapped[int] = mapped_column(ForeignKey("first_audit_runs.id"))
    url: Mapped[str]
    resource_type: Mapped[str]  # app.models.PageType value
    http_status: Mapped[int | None] = mapped_column(default=None)
    content_hash: Mapped[str | None] = mapped_column(default=None)
    # Hash of extracted JSON-LD structured data, populated in work-order step
    # 4 (fact extraction / JSON-LD round) - None until then, and None for any
    # page with no JSON-LD block at all.
    schema_hash: Mapped[str | None] = mapped_column(default=None)
    discovered_via: Mapped[str | None] = mapped_column(default=None)
    # Tier 2 blob references (nullable FK to Blob.hash) - only ever set for a
    # resource that earned its raw-body retention (a failing/needs-review
    # evaluation, or settings.keep_body_on_pass=True), per
    # app.evidence_store.store_resource_body_evidence, which is called once
    # rule evaluation results are known (work-order step 4), not during the
    # discovery pass this table is otherwise populated by. A passing
    # resource's row keeps content_hash/schema_hash (small, Tier 1) but both
    # of these stay None - no raw body is ever uploaded for it. Cleared back
    # to None (with the referenced Blob's refcount released) when the
    # retention job ages this resource's run out, independent of the rest of
    # the row, which is never pruned.
    body_blob_hash: Mapped[str | None] = mapped_column(ForeignKey("blobs.hash"), default=None)
    jsonld_blob_hash: Mapped[str | None] = mapped_column(ForeignKey("blobs.hash"), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ResourceFact(Base):
    """One extracted merchant fact (spec section 2.3's "Digital Twin"):
    { fact, value, source_url, source_text, method, confidence }, exactly as
    specced.

    variant_key: set only for a fact scoped to one specific offer/variant on
    a multi-offer product page (app.facts.jsonld_facts - the extracted,
    normalized SKU for that offer; None for anything else, including a
    product with no distinct offers). Exists so the contradiction engine
    (app.contradiction_engine) can key a per-resource product comparison by
    variant/SKU, not just by resource_id - comparing a page's single
    collapsed price against another source would silently pick the wrong
    variant's price on any page with more than one offer.
    """
    __tablename__ = "resource_facts"

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_id: Mapped[int] = mapped_column(ForeignKey("resources.id"))
    fact: Mapped[str]
    value: Mapped[str]
    source_url: Mapped[str]
    source_text: Mapped[str]
    method: Mapped[str]
    variant_key: Mapped[str | None] = mapped_column(default=None)
    confidence: Mapped[float]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Product(Base):
    """JSON-LD structured-data fields for one product resource (spec section
    2.4). One row per Resource of type PRODUCT. Populated in work-order step
    4 - table exists now, stays empty until the JSON-LD extractor is built.
    """
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_id: Mapped[int] = mapped_column(ForeignKey("resources.id"), unique=True)
    price: Mapped[str | None] = mapped_column(default=None)
    currency: Mapped[str | None] = mapped_column(default=None)
    availability: Mapped[str | None] = mapped_column(default=None)
    brand: Mapped[str | None] = mapped_column(default=None)
    gtin: Mapped[str | None] = mapped_column(default=None)
    mpn: Mapped[str | None] = mapped_column(default=None)
    sku: Mapped[str | None] = mapped_column(default=None)
    condition: Mapped[str | None] = mapped_column(default=None)
    raw_jsonld_json: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RuleRecord(Base):
    """DB mirror of one rule from app/rules/starter_rules.yaml. The YAML file
    is the source of truth - see app.rules.loader.sync_rules_to_db, which
    upserts every YAML rule here on startup and marks any DB row whose id is
    no longer present in the YAML as inactive (never hard-deleted, so
    historical Evaluation/Finding rows keep a valid foreign key). This table
    exists purely so Evaluation/Finding rows have a stable id to reference
    and so an admin screen can list rules from the DB - it is never hand-
    edited, and nothing here is treated as authoritative over the YAML.
    """
    __tablename__ = "rules"

    id: Mapped[str] = mapped_column(primary_key=True)  # the rule's own YAML `id`
    scope: Mapped[str]
    inputs_json: Mapped[str]
    condition_json: Mapped[str]
    impact: Mapped[str]
    severity: Mapped[str]
    description: Mapped[str]
    min_confidence: Mapped[float] = mapped_column(default=1.0)
    policy_reference: Mapped[str | None] = mapped_column(default=None)
    # The verified official Google page backing this rule - mirrors
    # Rule.google_source_url exactly (None when no verified page was found
    # for this rule; never a guess). See app.rules.loader's validation.
    google_source_url: Mapped[str | None] = mapped_column(default=None)
    active: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Evaluation(Base):
    """Result of evaluating one rule against one FirstAuditRun (and,
    for a product/resource-scoped rule, one specific Resource). Populated in
    work-order step 4 (the rule engine) - table exists from this round.
    """
    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    audit_run_id: Mapped[int] = mapped_column(ForeignKey("first_audit_runs.id"))
    rule_id: Mapped[str] = mapped_column(ForeignKey("rules.id"))
    resource_id: Mapped[int | None] = mapped_column(ForeignKey("resources.id"), default=None)
    result: Mapped[str]  # pass | fail | needs_review
    confidence: Mapped[float | None] = mapped_column(default=None)
    evidence_json: Mapped[str] = mapped_column(default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class FindingRecord(Base):
    """Normalized, queryable counterpart of app.models.Finding for one
    FirstAuditRun (spec deliverable: a `findings` migration). Written
    ALONGSIDE the existing AuditJobRecord/AuditRun.findings_json JSON blob,
    never instead of it - nothing that currently reads findings_json (report
    rendering, CSV export, the frontend) changes or breaks. This table is
    additive: it exists so an admin UI can query/filter/paginate individual
    findings directly instead of deserializing a JSON blob client-side.
    """
    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    audit_run_id: Mapped[int] = mapped_column(ForeignKey("first_audit_runs.id"))
    evaluation_id: Mapped[int | None] = mapped_column(ForeignKey("evaluations.id"), default=None)
    check_id: Mapped[str]
    title: Mapped[str]
    severity: Mapped[str]
    risk_level: Mapped[str]  # app.models.ImpactTier value
    page_url: Mapped[str | None] = mapped_column(default=None)
    store_evidence: Mapped[str] = mapped_column(default="")
    google_rule: Mapped[str | None] = mapped_column(default=None)
    consequence: Mapped[str | None] = mapped_column(default=None)
    remediation: Mapped[str | None] = mapped_column(default=None)
    google_source_link: Mapped[str | None] = mapped_column(default=None)
    # Per spec: screenshots are evidence only for critical/serious findings
    # (settings.screenshots_only_critical), saved as WebP via Tier 2 object
    # storage - never a bare PNG path on local disk. None for any finding that
    # wasn't screenshot-worthy, same "skipped rather than guessed" discipline
    # as the existing app.models.Finding.screenshot_path.
    screenshot_blob_hash: Mapped[str | None] = mapped_column(ForeignKey("blobs.hash"), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class User(Base):
    """Single-admin access control (GMC bot spec section 6). Seeded on
    startup from ADMIN_EMAIL/ADMIN_PASSWORD (app.auth.seed.seed_admin) - there
    is no sign-up flow, and `role` only ever has one real value in practice
    ("admin") today, kept as a string column rather than a hardcoded
    singleton so a future round can add a second, lower-privilege role
    without a migration.
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str]
    role: Mapped[str] = mapped_column(default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Session(Base):
    """Server-side session store (app.auth.sessions) - a revocable session
    cookie, not a stateless signed token, specifically so logout actually
    invalidates it rather than merely asking the client to forget it.
    `token` is the opaque value stored in the session cookie; looked up
    directly (it's generated via secrets.token_urlsafe, already
    unguessable - no separate hash-of-token step).
    """
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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
